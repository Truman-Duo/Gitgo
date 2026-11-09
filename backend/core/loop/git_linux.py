"""Linux trusted-Git configuration boundary, not a general tool sandbox.

POSIX advisory locks cannot prevent a hostile rename/write. Git therefore sees
sealed configuration bytes bound read-only at their original paths in a private
mount namespace. Live directory descriptors select the actual metadata objects.
The root view is read-only; only the Host-selected Git working/metadata/target
directories are writable. No unrestricted fallback is available.
"""
from contextlib import ExitStack
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys

from .git_execution import GitBoundaryError, _MAX_CONFIG


class GitCommand(list):
    def __init__(self, values, descriptors):
        super().__init__(values)
        self.pass_fds = tuple(sorted(set(descriptors)))


class LinuxPins:
    def __init__(self, owners, scratch):
        if sys.platform != 'linux' or not hasattr(os, 'memfd_create'):
            raise GitBoundaryError('GIT_BOUNDARY_UNAVAILABLE', 'Linux descriptor namespaces are unavailable')
        self.owners, self.scratch = owners, scratch
        self.directories, self.files = {}, {}
        self.additional_writable = []
        self.read_size = 0

    def directory(self, path):
        from backend.core.sandbox_read import ReadRoot
        path = Path(os.path.abspath(path))
        if path not in self.directories:
            root = self.owners.enter_context(ReadRoot(path))
            self.directories[path] = root._linux_directory_descriptor()
        return self.directories[path]

    def file(self, path, *, optional=False):
        from backend.core.sandbox_read import ReadRoot, ObjectIdentity, ReadBoundaryError
        path = Path(os.path.abspath(path))
        if path in self.files:
            return os.pread(self.files[path], _MAX_CONFIG + 1, 0)
        parent = self.directory(path.parent)
        try:
            with ReadRoot(path.parent) as root:
                info = os.fstat(parent)
                if root.identity != ObjectIdentity('linux', info.st_dev, info.st_ino):
                    raise GitBoundaryError('GIT_CONFIG_UNSUPPORTED', 'Selected Git metadata directory changed')
                with root.open(path.name) as opened:
                    raw = opened.read_bytes(max_bytes=_MAX_CONFIG)
        except FileNotFoundError:
            if optional:
                return None
            raise
        except ReadBoundaryError as error:
            raise GitBoundaryError('GIT_CONFIG_UNSUPPORTED', str(error)) from error
        self.read_size += len(raw)
        if self.read_size > _MAX_CONFIG or len(self.files) >= 256:
            raise GitBoundaryError('GIT_CONFIG_UNSUPPORTED', 'Metadata graph exceeds its bound')
        descriptor = os.memfd_create('gitgo-git-metadata', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        self.owners.callback(os.close, descriptor)
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if not count:
                raise OSError('Cannot capture Git metadata')
            view = view[count:]
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS,
                    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        self.files[path] = descriptor
        return raw

    def parser_path(self, path):
        # The fixed config parser reads the sealed bytes, without includes.
        # No credentials/configuration bytes are copied into disk scratch.
        return f'/proc/{os.getpid()}/fd/{self.files[Path(os.path.abspath(path))]}'

    def worktree_views(self, common):
        # Git prune decides whether registrations are dead by examining each
        # linked directory. A private /tmp must not turn live Host worktrees
        # into apparently missing paths and silently prune their registrations.
        from backend.core.sandbox_read import ReadRoot
        records = Path(common) / 'worktrees'
        try:
            selected = self.directory(records)
        except FileNotFoundError:
            return
        with ExitStack() as opened:
            readable = os.open(f'/proc/self/fd/{selected}', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            opened.callback(os.close, readable)
            names = os.listdir(readable)
            if len(names) > 256:
                raise GitBoundaryError('GIT_CONFIG_UNSUPPORTED', 'Worktree inventory exceeds its bound')
            with ReadRoot(records) as root:
                for name in names:
                    with root.open(f'{name}/gitdir') as metadata:
                        raw = metadata.read_bytes(max_bytes=32768).strip()
                    target = Path(os.fsdecode(raw))
                    if not target.is_absolute() or target.name != '.git':
                        raise GitBoundaryError('GIT_CONFIG_UNSUPPORTED', 'Worktree registration has an unsupported target')
                    try:
                        self.directory(target.parent)
                    except FileNotFoundError:
                        # A genuinely absent directory remains eligible for
                        # Git's normal expiration policy. Other errors refuse.
                        continue

    def wrap(self, command, *, cwd, writable):
        from backend.core.sandbox_linux import trusted_bwrap
        helper = trusted_bwrap(cwd)
        # Descriptor mounts were added in bubblewrap 0.11. Older helpers must
        # fail explicitly, never fall back to raceable /proc path mounts.
        try:
            capabilities = subprocess.run([helper, '--help'], capture_output=True,
                check=True, timeout=5, text=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            if not all(flag in capabilities.stdout.split() for flag in
                       ('--bind-fd', '--ro-bind-fd', '--ro-bind-data')):
                raise ValueError('descriptor mount support is missing')
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise GitBoundaryError('GIT_BOUNDARY_UNAVAILABLE',
                'Linux protected Git requires root-owned bubblewrap 0.11.1 or newer with descriptor mounts; '
                'install it through trusted system administration. No unrestricted fallback is allowed.') from error
        writable = [Path(os.path.abspath(path)) for path in [*writable, *self.additional_writable]]
        if any(path == Path(path.anchor) for path in writable):
            raise GitBoundaryError('GIT_BOUNDARY_UNAVAILABLE', 'A filesystem root cannot be a writable Git scope')
        for path in writable:
            self.directory(path)
        self.directory(self.scratch)
        result = [helper, '--die-with-parent', '--new-session', '--unshare-user',
                  '--unshare-pid', '--unshare-ipc', '--unshare-uts', '--cap-drop', 'ALL',
                  '--ro-bind', '/', '/', '--tmpfs', '/tmp']
        descriptors = []
        # An opened directory, not its mutable host name, selects every mount.
        # Ancestors precede descendants so later file overlays remain effective.
        for path, fd in sorted(self.directories.items(), key=lambda item: (len(item[0].parts), str(item[0]))):
            write = any(path == root or path.is_relative_to(root) for root in writable)
            result += ['--bind-fd' if write else '--ro-bind-fd', str(fd), str(path)]
            descriptors.append(fd)
        for path, fd in sorted(self.files.items()):
            os.lseek(fd, 0, os.SEEK_SET)
            # Anonymous memfds have no pathname to bind. bubblewrap copies
            # their sealed bytes into an anonymous readonly mount instead.
            result += ['--ro-bind-data', str(fd), str(path)]
            descriptors.append(fd)
        # bubblewrap consumes explicit mount descriptors and replaces procfs
        # before exec. No Host FD or Host procfs reaches the Git process.
        result += ['--proc', '/proc', '--dev', '/dev', '--chdir', str(cwd), '--', *command]
        # The private PID namespace also terminates descendants if its leader dies.
        return GitCommand(result, descriptors)


def writable_git_paths(cwd, gitdir, common, body):
    paths = [cwd, gitdir, common]
    if body[:2] == ['worktree', 'add']:
        # The worktree service emits exactly: add --detach TARGET COMMIT.
        if len(body) != 5 or body[2] != '--detach':
            raise GitBoundaryError('GIT_COMMAND_UNSUPPORTED', 'Unsupported worktree materialization arguments')
        paths.append(Path(body[3]).absolute().parent)
    elif body[:2] == ['worktree', 'remove']:
        if len(body) != 4 or body[2] != '--force':
            raise GitBoundaryError('GIT_COMMAND_UNSUPPORTED', 'Unsupported worktree retirement arguments')
        paths.append(Path(body[3]).absolute().parent)
    return paths
