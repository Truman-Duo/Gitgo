"""Ordinary-user bridge for an operator-provisioned, project-bound broker.

Candidate workspace execution and fixed object-only Git reads share this
bridge. Formal project writes remain the Host file service's responsibility.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import select
import shutil
import socket
import stat
import tempfile
import time

from backend.core.sandbox import SandboxDenied
from backend.core import macos_broker_protocol as wire

_SESSION=ContextVar('macos_broker_session',default=None)
_STAGE=ContextVar('macos_broker_candidate',default=None)


@contextmanager
def resident_project_session(service_name, project):
    """Bind an explicitly selected resident service to the Host project.

    The Host supplies the already registered project and the operator's
    service name. Neither value comes from a tool call. The service still
    verifies the live grant at every request.
    """
    from backend.core.macos_helper_install import trusted_directory
    from backend.core.macos_helper_lifecycle import service_root

    root = service_root(service_name)
    trusted_directory(root)
    with broker_session(root / 'client.json') as binding:
        if binding['socket'] != str(root / 'broker.sock') or 'project_object' not in binding:
            raise ValueError('Expected a resident project binding')
        project_binding(project)
        yield


@contextmanager
def broker_session(config):
    """Host/operator API, never accepted from tool JSON or environment."""
    config=Path(config).absolute()
    info=config.parent.lstat()
    if info.st_uid!=0 or info.st_mode & 0o022 or not stat.S_ISDIR(info.st_mode):
        raise ValueError('Broker endpoint directory must be root-owned')
    fd=os.open(config,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077 or info.st_size>8192:
            raise ValueError('Broker credential must be private to this user')
        value=json.loads(os.read(fd,8193))
    finally: os.close(fd)
    required={'socket','token','project','identity','grant_id'}
    if (type(value) is not dict or set(value) not in (required,required|{'project_object'})
            or type(value['socket']) is not str):
        raise ValueError('Invalid broker binding')
    if Path(value['socket']).parent!=config.parent:
        raise ValueError('Broker socket must share its pinned configuration directory')
    token=_SESSION.set(value)
    try: yield value
    finally: _SESSION.reset(token)


@contextmanager
def candidate_scope(project, stage):
    binding=_SESSION.get()
    if binding is None:
        yield
        return
    project=Path(project).absolute();stage=Path(stage).absolute()
    if str(project)!=binding['project'] or stage==project or stage.is_relative_to(project):
        raise SandboxDenied('SANDBOX_POLICY_INVALID','Broker requires the authorized project and a private execution view')
    fd=wire.open_directory(project)
    try:
        if wire.directory_identity(fd)!=binding['identity']:
            raise SandboxDenied('SANDBOX_POLICY_INVALID','Authorized project directory has been replaced')
        from backend.core.macos_project_identity import check_directory
        check_directory(fd,binding)
        info=stage.lstat();parent=stage.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or
                not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.getuid() or parent.st_mode & 0o022):
            raise SandboxDenied('SANDBOX_POLICY_INVALID','Candidate directory is not owned by this Host')
        token=_STAGE.set((binding,fd,stage,(info.st_dev,info.st_ino)))
        try: yield
        finally: _STAGE.reset(token)
    finally: os.close(fd)


def active(): return _SESSION.get() is not None


def bind_worker(target):
    """Carry only the project session into a Host-owned task thread."""
    binding = _SESSION.get()
    if binding is None:
        return target

    def run():
        token = _SESSION.set(binding)
        try:
            return target()
        finally:
            _SESSION.reset(token)

    return run


def read_git_objects(project, root, query, *, timeout=15, cancellation=None):
    """Compatibility interface for successful text-only object queries."""
    from backend.core.loop.git_execution import GitBoundaryError
    result=query_git_objects(project,root,query,timeout=timeout,cancellation=cancellation)
    if result.returncode:
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git predicate returned false')
    return result.stdout


def query_git_objects(project, root, query, *, timeout=15, cancellation=None):
    """Explicit Host-only adapter; not a tool handler or a production capability.

    Requires the operator's existing project grant and an already authorized
    native ReadRoot for exactly that project's .git/objects. Linked worktrees,
    external object directories and arbitrary Git commands remain unsupported.
    Uses the original native journal and status reconciliation, with no replay.
    """
    import math
    from backend.core import macos_git_read as git
    from backend.core.loop.execution_contract import NativeInvocation
    from backend.core.loop.native_invocation_scope import prepare_native_record, current_native_owner
    from backend.core.loop.git_execution import GitBoundaryError
    from backend.core.sandbox_read import ReadRoot
    binding=_SESSION.get()
    if binding is None or type(root) is not ReadRoot:
        raise SandboxDenied('SANDBOX_UNAVAILABLE','Authorized project broker and native object root required')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 1<=timeout<=60:
        raise ValueError('Invalid Git read timeout')
    project=Path(project).absolute()
    project_binding(project)
    from backend.core.storage.paths import resolve_existing_storage_paths
    if resolve_existing_storage_paths(project) is None:
        raise SandboxDenied('SANDBOX_UNAVAILABLE','Register the project before reading Git objects')
    git.read_command('/private/tmp/gitgo-query-validation',query)
    query=list(query)
    if cancellation is not None and cancellation.is_set():
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git read cancelled before capture')
    view=git.capture_objects(root,project/'.git/objects')
    objects=git.view_to_wire(view)
    project_binding(project)
    fd=wire.open_directory(project)
    observation={'launch_state':'not_started'};record=None
    try:
        if wire.directory_identity(fd)!=binding['identity']: raise ValueError('Project changed before admission')
        # Policy workspace identifies the existing journal's project, not a
        # filesystem grant to the child. Broker always allocates a fresh view.
        invocation=NativeInvocation.compile('macos_git_read',dict(_workspace=str(project),
            query=query,view_sha256=view.sha256),timeout,owner=current_native_owner(project))
        record=prepare_native_record(invocation,expected_workspace=project)
        observation['native_record']=record
        if cancellation is not None and cancellation.is_set():
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git read cancelled before launch')
        response=_exchange(binding,fd,dict(op='git_read',timeout=timeout,query=query,
            objects=objects,view_sha256=view.sha256),observation,cancellation,timeout)
        if not isinstance(response,dict):
            raise GitBoundaryError('GIT_READ_UNCONFIRMED',response.error)
        if response.get('reason')!='complete' or response.get('view_sha256')!=view.sha256 or 'image' in response:
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git result binding mismatch')
        project_binding(project)
        output=git.validate_output(query,response.get('stdout'),response.get('exit_code'))
    finally:
        os.close(fd)
        if record is not None: record.finish(observation)
    if record.failed:
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git result journal did not commit')
    if cancellation is not None and cancellation.is_set():
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git read cancelled before result delivery')
    return git.QueryResult(output,response['exit_code'])


def produce_git_objects(project, root, query, data, author=None, *, timeout=15, cancellation=None):
    """Produce immutable objects under the project grant, then append as Host.

    No branch, index or working file is changed. Lost outcomes are not replayed.
    """
    import base64
    import hashlib
    from backend.core import macos_git_write as write
    import math
    from backend.core import macos_git_read as git
    from backend.core.loop.execution_contract import NativeInvocation
    from backend.core.loop.native_invocation_scope import prepare_native_record, current_native_owner
    from backend.core.loop.git_execution import GitBoundaryError
    from backend.core.sandbox_read import ReadRoot
    binding=_SESSION.get()
    if binding is None or type(root) is not ReadRoot:
        raise SandboxDenied('SANDBOX_UNAVAILABLE','Authorized project broker and native object root required')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 1<=timeout<=60:
        raise ValueError('Invalid Git read timeout')
    project=Path(project).absolute()
    project_binding(project)
    from backend.core.storage.paths import resolve_existing_storage_paths
    if resolve_existing_storage_paths(project) is None:
        raise SandboxDenied('SANDBOX_UNAVAILABLE','Register the project before reading Git objects')
    write.validate(query,data,author)
    query=list(query)
    if cancellation is not None and cancellation.is_set():
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git read cancelled before capture')
    view=git.capture_objects(root,project/'.git/objects')
    objects=git.view_to_wire(view)
    project_binding(project)
    fd=wire.open_directory(project)
    observation={'launch_state':'not_started'};record=None
    try:
        if wire.directory_identity(fd)!=binding['identity']: raise ValueError('Project changed before admission')
        # Policy workspace identifies the existing journal's project, not a
        # filesystem grant to the child. Broker always allocates a fresh view.
        invocation=NativeInvocation.compile('macos_git_objects',dict(_workspace=str(project),
            query=query,view_sha256=view.sha256,input_sha256=hashlib.sha256(data).hexdigest(),author=author),timeout,owner=current_native_owner(project))
        record=prepare_native_record(invocation,expected_workspace=project)
        observation['native_record']=record
        if cancellation is not None and cancellation.is_set():
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git read cancelled before launch')
        response=_exchange(binding,fd,dict(op='git_objects',timeout=timeout,query=query,input=base64.b64encode(data).decode('ascii'),author=author,
            objects=objects,view_sha256=view.sha256),observation,cancellation,timeout)
        if not isinstance(response,dict):
            raise GitBoundaryError('GIT_READ_UNCONFIRMED',response.error)
        if response.get('reason')!='complete' or response.get('view_sha256')!=view.sha256 or 'image' in response:
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git result binding mismatch')
        project_binding(project)
        returned=response.get('objects')
        files,_=wire.validate_image(returned)
        after=git.ObjectView(tuple(sorted((n,raw) for n,raw,_ in files)),git.object_digest(sorted((n,raw) for n,raw,_ in files)))
        if returned['directories']:raise GitBoundaryError('GIT_READ_UNCONFIRMED','Unexpected object directories')
        produced=write.verify_result(view,after,query,data,author,response.get('stdout'))
    finally:
        os.close(fd)
        if record is not None: record.finish(observation)
    if record.failed:
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git result journal did not commit')
    if cancellation is not None and cancellation.is_set():
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Git read cancelled before result delivery')
    def guard():
        project_binding(project)
        if cancellation is not None and cancellation.is_set():
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Object import cancelled; no ref was updated')
    return git.QueryResult(write.append_objects(project,root,produced,guard=guard),0)


def checkout_git_commit(project, root, commit, *, timeout=30, cancellation=None):
    """Return a verified commit image, without publishing or registering it.

    Uses the existing grant and invocation journal. A lost outcome is never
    replayed and a successful leader exit alone cannot authorize the result.
    """
    import math
    from backend.core import macos_git_checkout as checkout, macos_git_read as git
    from backend.core.loop.execution_contract import NativeInvocation
    from backend.core.loop.native_invocation_scope import prepare_native_record, current_native_owner
    from backend.core.loop.git_execution import GitBoundaryError
    from backend.core.sandbox_read import ReadRoot
    from backend.core.storage.paths import resolve_existing_storage_paths
    binding=_SESSION.get()
    if binding is None or type(root) is not ReadRoot:
        raise SandboxDenied('SANDBOX_UNAVAILABLE','Authorized project broker and native object root required')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 1<=timeout<=60:
        raise ValueError('Invalid checkout timeout')
    checkout.validate_commit(commit)
    project=Path(project).absolute()
    def guard():
        project_binding(project)
        if cancellation is not None and cancellation.is_set():
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Checkout cancelled; no worktree was published')
    guard()
    if resolve_existing_storage_paths(project) is None:
        raise SandboxDenied('SANDBOX_UNAVAILABLE','Register the project before preparing a worktree')
    view=git.capture_objects(root,project/'.git/objects')
    objects=git.view_to_wire(view)
    guard()
    fd=wire.open_directory(project)
    observation={'launch_state':'not_started'};record=None
    try:
        if wire.directory_identity(fd)!=binding['identity']:
            raise ValueError('Project changed before checkout admission')
        invocation=NativeInvocation.compile('macos_git_checkout',dict(_workspace=str(project),
            commit=commit,view_sha256=view.sha256),timeout,owner=current_native_owner(project))
        record=prepare_native_record(invocation,expected_workspace=project)
        observation['native_record']=record
        guard()
        response=_exchange(binding,fd,dict(op='git_checkout',timeout=timeout,commit=commit,
            objects=objects,view_sha256=view.sha256),observation,cancellation,timeout)
        if not isinstance(response,dict):
            raise GitBoundaryError('GIT_READ_UNCONFIRMED',response.error)
        if (response.get('reason')!='complete' or response.get('view_sha256')!=view.sha256 or
                response.get('commit')!=commit or 'image' in response or 'objects' in response):
            raise GitBoundaryError('GIT_READ_UNCONFIRMED','Checkout result binding mismatch')
        guard()
        result=checkout.verify_result(commit,response.get('checkout'),response.get('stdout'))
    finally:
        os.close(fd)
        if record is not None:record.finish(observation)
    if record.failed:
        raise GitBoundaryError('GIT_READ_UNCONFIRMED','Checkout result journal did not commit')
    guard()
    return result


def project_binding(project):
    """Recheck the directory object at admission and existing commit guards."""
    binding=_SESSION.get()
    if binding is None: return None
    if str(Path(project).absolute())!=binding['project']:
        raise ValueError('Project does not match this capability')
    fd=wire.open_directory(project)
    try:
        if wire.directory_identity(fd)!=binding['identity']:
            raise ValueError('Authorized project directory has been replaced')
        from backend.core.macos_project_identity import check_directory
        check_directory(fd,binding)
    finally: os.close(fd)
    return {'grant_id':binding['grant_id'],'identity':binding['identity']}


def recovered_cleanup(item):
    """Authenticate a read-only receipt for this exact persisted invocation.

    No transport failure, absent record or stale grant is evidence of cleanup.
    A receipt never supplies command results or permission to replay.
    """
    from backend.core.macos_identity import boot_id, UID_MIN, UID_MAX
    binding=_SESSION.get()
    original=item.get('macos_broker_binding') or {}
    if binding is None or not original.get('grant_id'):
        return None
    if 'project_object' in binding:
        if original.get('project_object')!=binding['project_object']:return None
    elif original.get('project_identity')!=binding['identity']:return None
    project_binding(binding['project'])
    fd=wire.open_directory(binding['project'])
    connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    connection.settimeout(2)
    try:
        if wire.directory_identity(fd)!=binding['identity']: raise ValueError('Project changed')
        connection.connect(binding['socket'])
        if wire.peer_uid(connection)!=0: raise ValueError('Recovery peer is not root')
        wire.send_directory(connection,fd)
        request=dict(op='status',token=binding['token'],grant_id=binding['grant_id'],
            invocation_id=item['invocation_id'],execution_basis=item['execution_basis'])
        if original['grant_id']!=binding['grant_id']: request['original_grant_id']=original['grant_id']
        wire.send(connection,request)
        receipt=wire.receive(connection)
        expected=dict(event='status',invocation_id=item['invocation_id'],execution_basis=item['execution_basis'],
            grant_id=original['grant_id'],project_identity=original['project_identity'],owner_uid=os.getuid(),
            boot=boot_id(),outcome='unknown')
        if 'project_object' in binding:expected['project_object']=binding['project_object']
        if original.get('boot') is not None and original['boot']!=expected['boot']:
            expected['origin_boot']=original['boot']
        if set(receipt)!=set(expected)|{'uid','cleanup'} or any(receipt.get(k)!=v for k,v in expected.items()):
            raise ValueError('Recovery receipt binding mismatch')
        if type(receipt['uid']) is not int or not UID_MIN<=receipt['uid']<UID_MAX:
            raise ValueError('Invalid recovery identity')
        if receipt['cleanup']!=dict(complete=True,mode='macos_uid',invocation_id=item['invocation_id'],issues=[]):
            return None
        return receipt
    finally:
        connection.close();os.close(fd)


def replace_candidate(stage, identity, before, output):
    from backend.core.tools.workspace_projection import capture
    info=stage.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev,info.st_ino)!=identity or capture(stage)!=before:
        raise RuntimeError('Candidate changed during broker execution; outputs withheld')
    replacement=Path(tempfile.mkdtemp(prefix='broker-output-',dir=stage.parent))
    backup=Path(tempfile.mkdtemp(prefix='broker-input-',dir=stage.parent));backup.rmdir()
    try:
        wire.materialize(output,replacement)
        os.rename(stage,backup)
        try: os.rename(replacement,stage)
        except BaseException:
            os.rename(backup,stage);raise
        shutil.rmtree(backup)
    finally:
        if replacement.exists(): shutil.rmtree(replacement)


def run(invocation, input_data, observation, cancellation, timeout):
    from backend.core.loop.process_tool_runner import SubprocessResult
    from backend.core.macos_broker_capabilities import BrokerAdmissionError, BrokerCompatibilityError
    from backend.core.tools.workspace_projection import capture
    scope=_STAGE.get()
    if scope is None or scope[2]!=invocation.policy.workspace:
        raise SandboxDenied('SANDBOX_POLICY_INVALID','Native execution requires a bound candidate workspace')
    if input_data['tool_name']!='exec_command' or '_approved_read' in input_data:
        raise SandboxDenied('SANDBOX_UNAVAILABLE','Broker candidate currently accepts exec_command producers only')
    binding,fd,stage,identity=scope
    before=capture(stage)
    image=wire.image_wire(before)
    started=time.monotonic()
    try:
        response=_exchange(binding,fd,dict(op='run',timeout=timeout,request=input_data,image=image),
                           observation,cancellation,timeout)
        if not isinstance(response,dict): return response
        result=json.loads(response['stdout'])
        wire.validate_image(response['image'])
        project_binding(binding['project'])
        replace_candidate(stage,identity,before,response['image'])
        return SubprocessResult(success=result.get('success',False),data=result.get('data'),
            error=result.get('error',''),stderr=response.get('stderr',''),exit_code=0,
            duration_ms=(time.monotonic()-started)*1000)
    except (BrokerAdmissionError, BrokerCompatibilityError) as exc:
        # Preserve the structured refusal through the shared ToolPipeline.
        # These exceptions arise from negotiation or an explicit pre-launch
        # denial, never a lost acknowledgement or unknown execution outcome.
        raise SandboxDenied(exc.code,str(exc)) from exc
    except Exception as exc:
        if observation.get('cleanup') is not None: exc.cleanup=observation['cleanup']
        raise


def _exchange(binding,fd,request,observation,cancellation,timeout):
    """One authenticated launch/cleanup exchange for the original native record."""
    from backend.core.loop.process_tool_runner import SubprocessResult
    record=observation['native_record'];identifier=record.record['invocation_id']
    from backend.core.macos_identity import boot_id
    record.record['macos_broker_binding']=dict(grant_id=binding['grant_id'],project_identity=binding['identity'],boot=boot_id())
    if 'project_object' in binding:record.record['macos_broker_binding']['project_object']=binding['project_object']
    record._write()  # Durable association before any possible remote launch.
    request={**request,'token':binding['token'],'grant_id':binding['grant_id'],
             'invocation_id':identifier,'execution_basis':record.record['execution_basis']}
    start=time.monotonic();cancelled=False;timed_out=False
    connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    connection.settimeout(5)
    # Only the functional request enters the ambiguous launch window. Losing a
    # metadata-only handshake cannot have started a task on the service.
    try:
        connection.connect(binding['socket'])
        if wire.peer_uid(connection)!=0:
            raise SandboxDenied('SANDBOX_UNAVAILABLE','Broker peer is not root')
        wire.send_directory(connection,fd)
        from backend.core import macos_broker_capabilities as capabilities
        probe={key:request[key] for key in capabilities.CAPABILITY_REQUEST_FIELDS if key!='op'}
        probe['op']=capabilities.CAPABILITY_OPERATION
        wire.send(connection,probe)
        try:
            receipt=wire.receive_until(connection,start+min(5,timeout),cancellation=cancellation)
        except wire.ReceiveCancelled:
            return SubprocessResult(success=False,error='tool cancelled',effect_state='not_committed')
        except wire.ReceiveDeadlineExceeded:
            return SubprocessResult(success=False,error='tool timed out during helper negotiation',
                timed_out=True,effect_state='not_committed')
        capabilities.require_operation(receipt,request['op'],binding=binding,
            invocation_id=identifier,execution_basis=record.record['execution_basis'],
            owner_uid=os.getuid(),boot=record.record['macos_broker_binding']['boot'])
        cancelled=cancellation is not None and cancellation.is_set()
        timed_out=time.monotonic()-start>=timeout
        if cancelled or timed_out:
            return SubprocessResult(success=False,error='tool cancelled' if cancelled else 'tool timed out',
                timed_out=timed_out,effect_state='not_committed')
        observation['launch_state']='unknown'
        wire.send(connection,request)
        deadline=start+timeout+25
        while time.monotonic()<deadline:
            if not cancelled and not timed_out:
                cancelled=cancellation is not None and cancellation.is_set()
                timed_out=time.monotonic()-start>=timeout
                if cancelled or timed_out: wire.send(connection,{'op':'cancel'})
            if not select.select([connection],[],[],.05)[0]: continue
            response=wire.receive(connection)
            if response.get('invocation_id')!=identifier:
                raise RuntimeError('Unbound broker response')
            if response.get('event')=='started':
                if observation.get('started'): raise RuntimeError('Duplicate launch acknowledgement')
                pid=response.get('pid')
                if type(pid) is not int or pid<=1: raise RuntimeError('Invalid broker PID')
                observation['started']=True;record.started(pid)
                continue
            if response.get('event')=='denied':
                if observation.get('started'): raise RuntimeError('Denial after launch cannot prove not_started')
                # Broker guarantees denial frames are emitted only before a
                # durable invocation reservation/launch window was entered.
                observation['launch_state']='not_started'
                if 'code' in response:
                    capabilities.require_denial(response,binding,identifier,record.record['execution_basis'])
                return SubprocessResult(success=False,error=response['error'],effect_state='not_committed')
            if response.get('event')!='finished': raise RuntimeError('Unknown broker message')
            if not observation.get('started'): raise RuntimeError('Outcome without launch acknowledgement')
            cleanup=response.get('cleanup')
            if not isinstance(cleanup,dict) or cleanup.get('invocation_id')!=identifier:
                raise RuntimeError('Unbound cleanup receipt')
            if cleanup.get('mode')!='macos_uid': raise RuntimeError('Invalid cleanup mode')
            observation.update(cleanup=cleanup,exit_code=response.get('exit_code'))
            if cleanup.get('complete') is not True or cleanup.get('issues') or cleanup.get('mode')!='macos_uid':
                return SubprocessResult(success=False,error='SANDBOX_CLEANUP_PENDING',effect_state='ambiguous')
            # Cancellation/deadline may arrive while receiving the terminal
            # frame. A late success must not bypass the final delivery guard.
            cancelled=cancelled or (cancellation is not None and cancellation.is_set())
            timed_out=timed_out or time.monotonic()-start>=timeout
            if cancelled or timed_out:
                return SubprocessResult(success=False,error='tool cancelled' if cancelled else 'tool timed out',
                    timed_out=timed_out,effect_state='ambiguous',stderr=response.get('stderr',''))
            accepted_code=type(response.get('exit_code')) is int and response['exit_code']==0
            if request.get('op')=='git_read':
                from backend.core.macos_git_read import valid_exit_code
                accepted_code=valid_exit_code(request['query'],response.get('exit_code'))
            if not accepted_code:
                return SubprocessResult(success=False,error='Broker native process failed',effect_state='ambiguous',stderr=response.get('stderr',''))
            return response
        raise TimeoutError('Broker outcome is unknown; retain invocation evidence')
    except Exception as exc:
        # The common process runner has no local Popen for broker execution.
        # Preserve the remote launch window on transport/protocol errors so
        # it cannot misclassify a lost acknowledgement as a spawn failure.
        if observation.get('launch_state') in ('unknown','started') or observation.get('started'):
            exc.effect_state='ambiguous'
        # Invalid output/publication does not erase a bound cleanup receipt.
        # It still cannot turn the command's business effect into success.
        if observation.get('cleanup') is not None:
            exc.cleanup=observation['cleanup']
        raise
    finally: connection.close()
