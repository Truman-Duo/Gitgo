"""macOS execution-chain candidate, gated to an explicit root-owned trial.

The ordinary Host remains denied. No environment/JSON switch enables this
backend. A provisioned broker and complete isolation acceptance are still
required before removing the gate. Never run the Dashboard as root.
"""
from __future__ import annotations
import array
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os

# Pure validation/report helpers also import on non-POSIX hosts.
if os.name == 'posix':
    import fcntl

from pathlib import Path
import plistlib
import socket
import stat
import subprocess
import time
import uuid

from backend.core.sandbox import SandboxDenied, sandbox_environment, trusted_runtime_roots
from backend.core import macos_identity as identity
from backend.core.macos_file_authority import require_no_extended_acl

_TRIAL = ContextVar('macos_execution_trial', default=None)
STORE = Path('/private/tmp/gitgo-macos-execution-v1')
DURABLE_STORE = Path('/private/var/db/gitgo-execution-v1')
LOCK = '/private/tmp/gitgo-task-uid-probe-v1.lock'


def stage_source_service(root):
    """Seal the standalone service's import closure before preparing a lease.

    The frozen Host carries these modules in its bundle. Source trials use
    Python isolated mode and must carry every local dependency themselves.
    Import-only preflight runs before launchd registration or task admission.
    """
    for name in ('macos_identity.py', 'macos_execution_service.py',
                 'macos_resource_guard.py', 'macos_coalition.py', 'macos_file_authority.py'):
        target = root / name
        with target.open('xb') as stream:
            stream.write(Path(__file__).with_name(name).read_bytes())
        target.chmod(0o500)
    subprocess.run([identity.PYTHON, '-B', '-I', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);import macos_execution_service', str(root)],
        cwd='/', env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=10)
    return [identity.PYTHON, '-I', str(root / 'macos_execution_service.py'), str(root)]


def lease_root(binding):
    import re
    lease=binding.get('lease_id')
    if type(lease) is not str or not re.fullmatch('[0-9a-f]{32}',lease):
        raise ValueError('Invalid retained lease')
    kind=binding.get('lease_store','temporary')
    if kind not in ('temporary','persistent-v1'): raise ValueError('Unknown lease store')
    return (DURABLE_STORE if kind=='persistent-v1' else STORE)/lease


def private_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise RuntimeError('Execution state must be a private root-owned directory')
        require_no_extended_acl(fd)
    finally:
        os.close(fd)


def runtime_images():
    # Only already-loaded Host dependency images, not a Homebrew/home blanket
    # grant. The administrator trial trusts its selected runtime installation.
    import ctypes
    import sys
    if sys.platform != 'darwin':
        return []
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
    count = lib._dyld_image_count
    count.argtypes, count.restype = [], ctypes.c_uint32
    name = lib._dyld_get_image_name
    name.argtypes, name.restype = [ctypes.c_uint32], ctypes.c_char_p
    paths = set()
    for index in range(count()):
        raw = name(index)
        if raw:
            path = Path(os.fsdecode(raw))
            if path.is_absolute() and path.is_file():
                paths.update((str(path), str(path.resolve(strict=True))))
    return sorted(paths)


def profile(workspace, runtime_roots):
    """No network/Mach access, default-deny, inherited by fork/exec.

    File metadata is visible for runtime path lookup; content is allowlisted.
    This candidate is not a claim of aggregate resource or hostile fork bounds.
    """
    quote = lambda x: json.dumps(str(x), ensure_ascii=True)
    roots = ['/System', '/bin', '/usr/bin', '/usr/lib', '/usr/share',
             '/private/var/db/dyld', '/Library/Developer/CommandLineTools', *map(str, runtime_roots), str(workspace)]
    reads = ' '.join('(subpath %s)' % quote(x) for x in dict.fromkeys(roots))
    reads += ' ' + ' '.join('(literal %s)' % quote(x) for x in runtime_images())
    return ('(version 1) (deny default) '
            '(allow process-exec process-fork sysctl-read) '
            '(allow process-info* (target self)) (allow signal (target same-sandbox)) '
            '(allow file-read-metadata) '
            '(allow file-read* (literal "/") (literal "/dev/null") '
            '(literal "/dev/random") (literal "/dev/urandom") ' + reads + ') '
            '(allow file-map-executable ' + reads + ') '
            '(allow file-write* (subpath ' + quote(workspace) + ') (literal "/dev/null")) '
            '(deny file-write-setugid)')


@contextmanager
def execution_trial(workspace, *, persistent=False, wall_seconds=None):
    """Administrator-only opt-in for one disposable workspace/one invocation.

    A permanent UID reservation is committed before handing workspace ownership
    to the task. Even clean identities stay quarantined; corrupt debt blocks
    further trials. Does not touch an existing project or install a daemon.
    """
    import sys
    if sys.platform != 'darwin' or os.getuid() != 0 or os.geteuid() != 0:
        raise SandboxDenied('SANDBOX_UNAVAILABLE', 'macOS execution trial requires an administrator harness')
    if wall_seconds is not None and (type(wall_seconds) not in (int,float) or not 0<wall_seconds<=1800):
        raise ValueError('Invalid wall-time deadline')
    workspace = Path(workspace).absolute()
    if (workspace.parent.parent != Path('/private/tmp') or
            not workspace.parent.name.startswith('gitgo-macos-chain-')):
        raise ValueError('Trial requires its disposable workspace')
    private_directory(workspace.parent)
    private_directory(workspace)
    if any(workspace.iterdir()):
        raise ValueError('Trial workspace must initially be empty')
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    owner_fd = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError('Unsafe identity allocation lock')
        require_no_extended_acl(fd)
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SandboxDenied('SANDBOX_UNAVAILABLE', 'Identity allocator is busy') from None
                time.sleep(.02)
        store=DURABLE_STORE if persistent else STORE
        if persistent:
            from backend.core.macos_helper_install import trusted_directory
            trusted_directory(store.parent)
        store.mkdir(mode=0o700, exist_ok=True)
        private_directory(store)
        used = set()
        for history in (STORE,DURABLE_STORE):
            if not history.exists(): continue
            private_directory(history)
            for directory in history.iterdir():
                private_directory(directory)
                record = load_state(directory)
                used.add(record['uid'])
                if record['phase'] != 'clean' and not live_lease_owner(directory, record):
                    raise SandboxDenied('SANDBOX_CLEANUP_PENDING', 'An earlier macOS trial requires recovery: ' + str(directory))
        # Earlier administrator prototypes share the allocation lock. Retain
        # their reservations too, even if their owner died before task exec.
        for old in Path('/private/tmp').glob('gitgo-uid-recovery-*'):
            info = old.lstat()
            if info.st_uid != 0:
                continue
            old_fd=os.open(old,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
            try:
                opened=os.fstat(old_fd)
                if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid!=0 or opened.st_mode&0o022
                        or (opened.st_dev,opened.st_ino)!=(info.st_dev,info.st_ino)):
                    raise RuntimeError('Untrusted prior prototype identity record')
                require_no_extended_acl(old_fd)
            finally: os.close(old_fd)
            try:
                private_directory(old/'control')
                prior = json.loads(identity.bounded_read(old / 'control/state.json', 0, True))
            except FileNotFoundError:
                continue
            old_uid = prior.get('uid')
            if type(old_uid) is not int or not identity.UID_MIN <= old_uid < identity.UID_MAX:
                raise RuntimeError('Invalid prior prototype identity')
            used.add(old_uid)
        uid = identity.choose_uid(used)
        root = store / uuid.uuid4().hex
        root.mkdir(mode=0o700)
        owner_fd = lease_owner_lock(root, create=True)
        fcntl.flock(owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = dict(schema=1, uid=uid, boot_id=identity.boot_id(), phase='reserved',
                     launch_count=0, invocation_id='', generation=0, workspace=str(workspace))
        identity.atomic_json(root / 'state.json', state)
        os.chown(workspace, uid, uid)
        # Parent traversal is required by task UID, but it cannot rename work.
        workspace.parent.chmod(0o711)
        # Allocation/reservation is atomic, but execution belongs to this
        # lease, not the global allocator. A crashed owner releases its lock;
        # unfinished orphan leases still block admission until recovered.
        fcntl.flock(fd, fcntl.LOCK_UN)
        token = _TRIAL.set(dict(root=root, workspace=workspace, uid=uid, used=False,wall_seconds=wall_seconds))
        try:
            yield _TRIAL.get()
        finally:
            _TRIAL.reset(token)
            current = load_state(root)
            if current['phase'] == 'reserved':
                current['phase'] = 'clean'  # No command capsule ever committed.
                identity.atomic_json(root / 'state.json', current)
    finally:
        if owner_fd is not None:
            os.close(owner_fd)
        os.close(fd)


def lease_owner_lock(root, *, create=False):
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(root / 'admission.lock', flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
                info.st_nlink != 1 or info.st_mode & 0o077):
            raise RuntimeError('Unsafe lease owner lock')
        require_no_extended_acl(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def live_lease_owner(root, record):
    # This is a liveness exception for concurrent admission, never proof of
    # cleanup. Legacy, orphaned, cross-boot and cleanup-pending records block.
    if (record['phase'] not in ('reserved', 'prepared', 'starting', 'running') or
            record.get('boot_id') != identity.boot_id()):
        return False
    try:
        fd = lease_owner_lock(root)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            current = load_state(root)
            return (current['uid'] == record['uid'] and
                    current.get('boot_id') == record['boot_id'] and
                    current['phase'] in ('reserved', 'prepared', 'starting', 'running', 'clean'))
        return False
    finally:
        os.close(fd)


def load_state(root):
    private_directory(root)
    s = json.loads(identity.bounded_read(root / 'state.json', 0, True))
    if (type(s) is not dict or s.get('schema') != 1 or
            type(s.get('uid')) is not int or not identity.UID_MIN <= s['uid'] < identity.UID_MAX or
            s.get('phase') not in ('reserved', 'prepared', 'starting', 'running', 'cleanup_pending', 'clean') or
            type(s.get('launch_count')) is not int or s['launch_count'] not in (0, 1) or
            type(s.get('generation')) is not int or not 0 <= s['generation'] <= 100):
        raise RuntimeError('Invalid UID lease; retain recovery debt')
    if s['phase'] in ('reserved', 'prepared') and s['launch_count']:
        raise RuntimeError('Invalid prelaunch lease')
    return s


def popen(argv, policy, *, invocation_id="", **kwargs):
    if getattr(policy,'read_roots',()) or getattr(policy,'metadata_roots',()):
        raise SandboxDenied('SANDBOX_UNAVAILABLE', 'macOS admitted read views are not implemented; no implicit filesystem grant')
    trial = _TRIAL.get()
    if trial is None:
        raise SandboxDenied('SANDBOX_UNAVAILABLE',
            'macOS execution broker is not provisioned and validated; no unsandboxed fallback')
    if trial['used'] or policy.workspace != trial['workspace']:
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Trial cannot change or reuse its workspace lease')
    allowed = {'cwd', 'stdin', 'stdout', 'stderr', 'text', 'encoding', 'errors', 'env', 'creationflags', 'start_new_session'}
    if set(kwargs) - allowed or any(kwargs.get(s) != subprocess.PIPE for s in ('stdin','stdout','stderr')):
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Private stdio pipes are mandatory')
    if not argv or not all(type(a) is str and '\0' not in a for a in argv) or not Path(argv[0]).is_absolute():
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Expected an absolute Host command')
    cwd = Path(kwargs['cwd']).resolve(strict=True)
    roots = trusted_runtime_roots()
    if cwd != policy.workspace and cwd not in roots:
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Untrusted execution cwd')
    import re
    if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Durable invocation identity required')
    trial['used'] = True
    return DarwinTrialProcess(trial, argv, policy, kwargs, roots, invocation_id)


def task_environment(argv, workspace, source):
    env = sandbox_environment(source)
    if any(key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_')) for key in source):
        from backend.core.macos_git_write import identity_environment
        env.update(identity_environment(argv, source, workspace))
    env.update(HOME=str(workspace), TMPDIR=str(workspace),
               USERPROFILE=str(workspace), GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL='/dev/null')
    return env


class DarwinTrialProcess:
    def __init__(self, trial, argv, policy, options, roots, invocation_id):
        self.root, self.args = trial['root'], argv
        self.invocation_id = invocation_id
        self.boot = identity.boot_id()
        self.uid = trial["uid"]
        self.pid, self.returncode = 0, None
        self.stdin = self.stdout = self.stderr = None
        self.control = None
        self._gitgo_macos_scope = self
        self._gitgo_cleanup = {'complete': False, 'mode': 'macos_uid', 'issues': ['cleanup_not_observed']}
        self.label = 'org.gitgo.execution-trial.' + self.root.name
        self.loaded = False
        descriptors = []
        try:
            from backend.core.child_process import is_frozen_runtime, INTERNAL_ROLE_FLAG
            import sys
            if is_frozen_runtime():
                service_command=[sys.executable,INTERNAL_ROLE_FLAG,'macos-execution-service',str(self.root)]
            else:
                service_command = stage_source_service(self.root)
            env = task_environment(argv, policy.workspace, options.get('env', {}))
            if is_frozen_runtime():
                node_bin=Path(sys.executable).parent/'developer-tools/bin'
                if (node_bin/'node').is_file():
                    env['PATH']=str(node_bin)+':/usr/bin:/bin'
                    env['OPENSSL_CONF']='/dev/null'
            from backend.core.macos_resource_guard import validate
            resources=validate(dict(memory_bytes=policy.memory_bytes,process_limit=policy.process_limit,cpu_seconds=policy.cpu_seconds))
            state = load_state(self.root)
            state.update(phase='prepared', invocation_id=invocation_id,
                         argv=argv, cwd=options['cwd'], env=env,
                         profile=profile(policy.workspace, roots), timeout=trial.get('wall_seconds') or policy.cpu_seconds,resources=resources,
                         clock='CLOCK_MONOTONIC_RAW')
            identity.atomic_json(self.root / 'state.json', state)
            job = dict(Label=self.label, ProgramArguments=service_command,
                KeepAlive={'SuccessfulExit': False}, RunAtLoad=True, ThrottleInterval=1,
                ExitTimeOut=3, Umask=0o077,
                StandardOutPath=str(self.root / 'service.out'), StandardErrorPath=str(self.root / 'service.err'))
            plist = self.root / 'job.plist'
            plist.write_bytes(plistlib.dumps(job)); plist.chmod(0o600)
            self.loaded = True
            subprocess.run(['/bin/launchctl', 'bootstrap', 'system', str(plist)],
                           capture_output=True, check=True, timeout=8)
            self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.control.settimeout(8)
            end = time.monotonic() + 8
            while True:
                try:
                    self.control.connect(str(self.root / 'owner.sock')); break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.monotonic() >= end: raise TimeoutError('macOS supervisor unavailable')
                    time.sleep(.03)
            i_r,i_w = os.pipe(); descriptors += [i_r,i_w]
            o_r,o_w = os.pipe(); descriptors += [o_r,o_w]
            e_r,e_w = os.pipe(); descriptors += [e_r,e_w]
            self.control.sendmsg([b'GO'], [(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[i_r,o_w,e_w]))])
            for fd in (i_r,o_w,e_w): os.close(fd); descriptors.remove(fd)
            ready = bytearray()
            while len(ready) < 128 and not ready.endswith(b'\n'):
                byte = self.control.recv(1)
                if not byte: break
                ready.extend(byte)
            if not ready.startswith(b'PID:') or not ready.endswith(b'\n'):
                raise RuntimeError('No launch acknowledgement')
            self.pid = int(ready[4:-1])
            if self.pid <= 1: raise RuntimeError('Invalid task PID')
            # Match Popen's binary/text selection. In particular Git blob input
            # must never pass through UTF-8 decoding or newline translation.
            text_mode = bool(options.get('text') or options.get('encoding') or options.get('errors'))
            stream_options = dict(encoding=options.get('encoding') or 'utf-8',
                                  errors=options.get('errors') or 'strict') if text_mode else {}
            for name,fd,mode in [('stdin',i_w,'w'),('stdout',o_r,'r'),('stderr',e_r,'r')]:
                stream = os.fdopen(fd, mode if text_mode else mode+'b', **stream_options)
                descriptors.remove(fd); setattr(self,name,stream)
        except BaseException as exc:
            if self.control: self.control.close(); self.control = None
            for fd in descriptors: os.close(fd)
            for name in ('stdin', 'stdout', 'stderr'):
                stream = getattr(self, name, None)
                if stream is not None: stream.close()
            # A disconnected/failed launch may have run. Do not claim not_started,
            # unload its recovery service, or recycle its UID based on this error.
            denial = SandboxDenied('SANDBOX_CLEANUP_PENDING', 'macOS launch outcome requires recovery: ' + str(self.root))
            denial.effect_state = 'ambiguous'
            denial.cleanup = self._gitgo_cleanup
            raise denial from exc

    def poll(self):
        state = load_state(self.root)
        if state['boot_id'] != self.boot or state['invocation_id'] != self.invocation_id or state['uid'] != self.uid:
            raise RuntimeError('Recovery identity changed')
        if state['phase'] == 'clean':
            cleanup = state.get('cleanup', {})
            self._gitgo_cleanup = dict(complete=cleanup.get('observed_empty') is True and not cleanup.get('issues'),
                mode='macos_uid', issues=cleanup.get('issues') or [], invocation_id=state['invocation_id'],
                uid=state['uid'], generation=state['generation'])
            self.returncode = state.get('exit_code') if type(state.get('exit_code')) is int else -1
            self.resource_reason=state.get('reason','') if state.get('reason','').startswith('resource_') else None
            if self.resource_reason:self.returncode=-1
        return self.returncode

    def wait(self, timeout=None):
        end = time.monotonic() + (timeout if timeout is not None else 15)
        while self.poll() is None:
            if time.monotonic() >= end: raise subprocess.TimeoutExpired(self.args,timeout)
            time.sleep(.04)
        return self.returncode

    def kill(self):
        if self.control:
            try: self.control.sendall(b'CANCEL')
            except OSError: pass  # launchd recovery still owns the UID.

    terminate = kill

    def close_sandbox(self):
        if self.returncode is None:
            self.kill()
        try:
            self.wait(15)
            if self._gitgo_cleanup['complete'] and self.loaded:
                subprocess.run(['/bin/launchctl','bootout','system/'+self.label],
                               capture_output=True,check=True,timeout=8)
                self.loaded = False
        finally:
            if self.control: self.control.close(); self.control = None
