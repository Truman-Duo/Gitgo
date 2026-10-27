"""Bounded, noninteractive Git plumbing for the trusted worktree service.

Repository code is not trusted service code. In particular, add/checkout/hash
may run filters, and --no-verify does not disable all hooks. Windows pins the
configuration objects against writes/replacement through the whole invocation;
other platforms need an equivalent backend before using this trusted path.
This is not a general-purpose shell or a replacement for the native runner.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


class GitBoundaryError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


CONTENT_READ_COMMANDS = frozenset({"rev-parse", "symbolic-ref", "diff", "ls-files", "cat-file", "show", "ls-tree", "log",
                                   "status", "rev-list", "for-each-ref", "merge-base", "grep", "diff-tree"})
LOCAL_WRITE_COMMANDS = frozenset({"apply", "add", "reset", "commit", "update-ref", "hash-object", "rm", "branch", "cherry-pick", "mktree", "commit-tree", "merge-tree"})
_ALLOWED = CONTENT_READ_COMMANDS | LOCAL_WRITE_COMMANDS | {"worktree", "fetch", "push", "ls-remote"}
# Global preferences that affect business bytes/path handling, never commands.
_DATA_SETTINGS = frozenset({"core.autocrlf", "core.eol", "core.safecrlf",
    "core.filemode", "core.symlinks", "core.ignorecase", "core.precomposeunicode",
    "core.protecthfs", "core.protectntfs", "core.attributesfile", "core.excludesfile",
    "i18n.commitencoding", "user.name", "user.email", "user.useconfigonly"})
_MAX_CONFIG = 4 * 1024 * 1024


def _environment():
    # No inherited alternate index/object store, executable, injected -c, pager,
    # tracing destination, transport or credential-program override.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("GIT_", "GCM_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never",
               GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1",
               GIT_ATTR_NOSYSTEM="1", GIT_PAGER="", GIT_EDITOR="",
               GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1")
    return env


def _executable(cwd: Path):
    # Do not let Windows' implicit current-directory executable search select
    # a project-supplied git.exe. PATH itself remains operator configuration.
    name = "git.exe" if os.name == "nt" else "git"
    for entry in os.get_exec_path():
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        directory = directory.resolve()
        if directory == cwd or cwd in directory.parents:
            continue
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    raise GitBoundaryError("GIT_EXECUTABLE_UNAVAILABLE", "trusted PATH has no Git executable")


def _invoke(command, *, cwd, env, input_bytes=None, timeout=120):
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True,
        check=False, timeout=timeout, close_fds=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **({"pass_fds": command.pass_fds} if os.name == 'posix' and hasattr(command, 'pass_fds') else {}),
        **({"input": input_bytes} if input_bytes is not None
           else {"stdin": subprocess.DEVNULL}))



def discover_git_metadata(cwd, flag):
    """Fixed metadata discovery; inherited GIT_* never chooses project identity.

    This builtin does not materialize worktrees or execute repository programs.
    It also works before an identity exists and for an ordinary nested directory.
    """
    if flag not in {"--git-common-dir", "--show-toplevel"}:
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Unsupported Git metadata discovery")
    cwd = Path(cwd).absolute()
    return _invoke([_executable(cwd), "rev-parse", flag], cwd=cwd,
                   env=_environment(), timeout=10)


def _entries(raw):
    if len(raw) > _MAX_CONFIG:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "effective configuration is too large")
    try:
        for entry in raw.split(b"\0"):
            if entry:
                key, _, value = entry.partition(b"\n")
                yield key.decode("utf-8"), value.decode("utf-8")
    except UnicodeError as exc:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "configuration is not UTF-8") from exc


def _read_config(git, path, *, cwd, env, pins, seen=None, depth=0, forbidden_roots=()):
    path = Path(os.path.abspath(path))
    if any(path.is_relative_to(root) for root in forbidden_roots):
        raise GitBoundaryError("GIT_REPOSITORY_EXTENSION_UNSUPPORTED", "Operator Git configuration cannot be sourced from a managed repository")
    seen = set() if seen is None else seen
    if path in seen:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "repeated/cyclic repository includes exceed the protected graph contract")
    if depth > 10 or len(seen) >= 128:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "repository include graph exceeds its bound")
    pins.file(path)
    seen.add(path)
    parser_path = pins.parser_path(path) if hasattr(pins, 'parser_path') else str(path)
    result = _invoke([git, "config", "--file", parser_path, "--no-includes",
                       "--null", "--list"], cwd=cwd, env=env, timeout=15)
    if result.returncode:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "cannot parse the pinned configuration")
    entries = list(_entries(result.stdout))
    graph = list(entries)
    for key, value in entries:
        if key != "include.path" and not (key.startswith("includeif.") and key.endswith(".path")):
            continue
        # Protect every potential branch, not only today's matching condition.
        # A missing include cannot be locked against appearing later.
        if not value or "%(" in value:
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "repository include path requires unsupported expansion")
        include = Path(os.path.expanduser(value))
        if not include.is_absolute():
            include = path.parent / include
        try:
            graph.extend(_read_config(git, include, cwd=cwd, env=env,
                                      pins=pins, seen=seen, depth=depth + 1, forbidden_roots=forbidden_roots))
        except FileNotFoundError as exc:
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "missing repository include cannot be protected") from exc
    return graph


class _WindowsPins:
    def __init__(self, owners):
        from backend.core.sandbox_read_windows import WindowsReadApi
        self.api, self.owners = WindowsReadApi(), owners
        self.read_size = 0

    def directory(self, path):
        handle = self.api.root(path)
        self.owners.callback(self.api.close, handle)
        self.api.identity(handle, directory=True)
        return handle

    def file(self, path, *, optional=False):
        import msvcrt
        parent = self.directory(path.parent)
        try:
            handle = self.api.open(path.name, parent, directory=False, share_write=False)
        except FileNotFoundError:
            if optional:
                return None
            raise
        try:
            self.api.identity(handle, directory=False)
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            self.api.close(handle)
            raise
        self.owners.callback(os.close, fd)
        raw = os.read(fd, _MAX_CONFIG + 1)
        self.read_size += len(raw)
        if self.read_size > _MAX_CONFIG:
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "metadata graph exceeds the read bound")
        return raw


def _metadata(cwd, pins, *, protect_config=True):
    pins.directory(cwd)
    marker = cwd / ".git"
    if marker.is_dir():
        gitdir = marker
        pins.directory(gitdir)
    else:
        raw = pins.file(marker)
        if not raw.startswith(b"gitdir: ") or len(raw) > 32768:
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "expected a bounded Git directory reference")
        gitdir = Path(os.fsdecode(raw[8:].strip()))
        if not gitdir.is_absolute():
            gitdir = cwd / gitdir
        # root() rejects reparse traversal before resolve could erase it.
        gitdir = Path(os.path.abspath(gitdir))
        pins.directory(gitdir)
        gitdir = gitdir.resolve()
    raw = pins.file(gitdir / "commondir", optional=True)
    common = gitdir
    if raw is not None:
        name = Path(os.fsdecode(raw.strip()))
        common = name if name.is_absolute() else gitdir / name
        common = Path(os.path.abspath(common))
        pins.directory(common)
        common = common.resolve()
    if protect_config:
        pins.file(common / "config")
    return gitdir, common


def _arguments(args):
    if not args or args[0] != "git":
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "worktree service only runs Git")
    body = list(args[1:])
    index = 0
    while index < len(body) and body[index] == "-c":
        if index + 1 >= len(body) or not body[index + 1].startswith(("user.name=", "user.email=")):
            raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "only internal commit identity may be overridden")
        index += 2
    if index >= len(body) or body[index] not in _ALLOWED:
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "command is outside worktree plumbing")
    if body[index] == "symbolic-ref" and (len(body[index:]) != 3 or body[index + 1] != "--quiet"
            or not (body[index + 2] == "HEAD" or body[index + 2].startswith("refs/"))):
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Only an exact symbolic reference observation is supported")
    options = body[index + 1:]
    if body[index] == "mktree" and options != ["-z"]:
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Only explicit tree input is supported")
    full_oid = lambda value: bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value))
    if body[index] == "commit-tree":
        parents = options[1:-2]
        if (not 3 <= len(options) <= 21 or options[-2] != "-m" or not full_oid(options[0])
                or len(parents) % 2 or any(parents[n] != "-p" or not full_oid(parents[n + 1])
                    for n in range(0, len(parents), 2))):
            raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Only a fixed tree, zero to nine fixed parents and a message are supported")
    if body[index] == "merge-tree":
        fixed = options[4:]
        if fixed and fixed[0].startswith("--merge-base=") and full_oid(fixed[0].split("=", 1)[1]):
            fixed = fixed[1:]
        if (options[:4] != ["--write-tree", "--name-only", "--no-messages", "-z"]
                or len(fixed) != 2 or not all(full_oid(value) for value in fixed)):
            raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Only a fixed two-head merge preview is supported")
    if "--" in options:
        options = options[:options.index("--")]
    if any(part in {"--ext-diff", "--textconv"} or part.startswith(("--ext-diff=", "--textconv=")) for part in options):
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Executable diff transformations are outside managed Git reads")
    if body[index] in {"diff", "show", "log", "diff-tree"}:
        body[index + 1:index + 1] = ["--no-ext-diff", "--no-textconv"]
    return body


@contextmanager
def protected_git(cwd, args, *, transport_identity=None, _read_session=False):
    """Yield a fixed command/environment while configuration objects are pinned.

    The service may still perform its intended filesystem effects. This closes
    executable Git extensions, not arbitrary filesystem races or semantic review.
    """
    body = _arguments(args)
    if _read_session and (body != ["cat-file", "--batch"] or transport_identity is not None):
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "A read session cannot establish transport or write authority")
    if os.name != "nt" and sys.platform != 'linux':
        raise GitBoundaryError("GIT_BOUNDARY_UNAVAILABLE", "protected trusted Git backend is unavailable; no unrestricted fallback")
    cwd = Path(cwd).absolute()
    git = _executable(cwd)
    with ExitStack() as owners:
        scratch = Path(owners.enter_context(tempfile.TemporaryDirectory(prefix="gitgo-git-")))
        if os.name == 'nt':
            pins = _WindowsPins(owners)
        else:
            from .git_linux import LinuxPins
            pins = LinuxPins(owners, scratch)
        gitdir, common = _metadata(cwd, pins)
        if scratch.is_relative_to(cwd) or scratch.is_relative_to(common):
            raise GitBoundaryError("GIT_BOUNDARY_UNAVAILABLE", "private scratch must be outside the execution and repository metadata trees")
        env = _environment()
        env.update(GIT_DIR=str(gitdir), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(cwd))
        # The config builtin only parses data; it never runs filters/hooks.
        # First pin the repository include graph, then capture effective
        # operator preferences/driver names. No raw credentials are persisted.
        local = _read_config(git, common / "config", cwd=scratch, env=env, pins=pins)
        if any(k == "extensions.worktreeconfig" for k, _ in local) and hasattr(pins, 'parser_path'):
            # Every potentially active include branch is already in `local`.
            # Do not decide whether to capture per-worktree config by reading
            # mutable host names again. This narrow backend requires that file
            # whenever the extension is declared, even in an inactive branch.
            try:
                local.extend(_read_config(git, gitdir / 'config.worktree', cwd=scratch, env=env, pins=pins))
            except FileNotFoundError as error:
                raise GitBoundaryError('GIT_CONFIG_UNSUPPORTED',
                    'Declared worktree configuration requires an existing snapshot; missing names cannot be protected') from error
        elif any(k == "extensions.worktreeconfig" for k, _ in local):
            extension = _invoke([git, "config", "--file", str(common / "config"),
                "--includes", "--type=bool", "--get", "extensions.worktreeconfig"],
                cwd=cwd, env=env, timeout=15)
            if extension.returncode not in {0, 1}:
                raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "invalid worktree config extension")
            if extension.stdout.strip() == b"true":
                pins.file(gitdir / "config.worktree")
                local.extend(_read_config(git, gitdir / "config.worktree", cwd=scratch, env=env, pins=pins))
        bootstrap = dict(env)
        bootstrap.pop("GIT_CONFIG_NOSYSTEM", None)
        result = _invoke([git, "config", "--null", "--list"], cwd=cwd, env=bootstrap, timeout=15)
        if result.returncode:
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "effective configuration could not be read")
        entries = list(_entries(result.stdout))
        # Freeze global/system content preferences; local config remains pinned.
        snapshot = scratch / "preferences"
        values = {k: v for k, v in entries if k.lower() in _DATA_SETTINGS}
        lines = []
        for key, value in values.items():
            section, name = key.split(".", 1)
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
            lines.append(f'[{section}]\n{name} = "{escaped}"\n')
        snapshot.write_text("".join(lines), encoding="utf-8")
        env.update(GIT_CONFIG_GLOBAL=str(snapshot), GIT_CONFIG_SYSTEM=os.devnull)
        hooks = scratch / "hooks"
        hooks.mkdir()
        settings = [("core.longpaths", "true"), ("core.hooksPath", str(hooks)),
            ("core.fsmonitor", "false"), ("core.pager", ""),
            ("commit.gpgSign", "false"), ("tag.gpgSign", "false"), ("log.showSignature", "false"),
            ("gc.auto", "0"), ("maintenance.auto", "false"),
            ("submodule.recurse", "false"), ("protocol.allow", "never")]
        from backend.core.git_transfer import TRANSPORT_COMMANDS, prepare_transfer
        if body[0] in TRANSPORT_COMMANDS:
            settings.extend(prepare_transfer(git, cwd, body, bootstrap=bootstrap, env=env,
                local=local, entries=entries, pins=pins, gitdir=gitdir, common=common, identity=transport_identity))
        subcommand = next(part for part in body if part in _ALLOWED)
        if subcommand in {"cherry-pick", "merge-tree"} and any(re.fullmatch(r"merge\..+\.driver", key) and value for key, value in [*entries, *local]):
            raise GitBoundaryError("GIT_REPOSITORY_EXTENSION_UNSUPPORTED", "Custom merge programs are unsupported for managed merges")
        drivers = set()
        # A nested checkout can match a different includeIf condition. Disable
        # drivers from every protected branch, not just the bootstrap context.
        for key, _ in [*entries, *local]:
            match = re.fullmatch(r"filter\.(.+)\.(clean|smudge|process|required)", key)
            if match:
                drivers.add(match[1])
        # An active custom filter must fail explicitly, rather than transparently
        # stage unfiltered bytes and claim to preserve the repository's meaning.
        for driver in sorted(drivers):
            for kind in ("clean", "smudge", "process"):
                settings.append((f"filter.{driver}.{kind}", ""))
            settings.append((f"filter.{driver}.required", "true"))
        active = True
        def command_for(arguments):
            # Transfer preparation can append receiver safety arguments to the
            # already validated body. Keep that exact body for ordinary calls.
            selected = body if arguments is None else _arguments(arguments)
            if not active or (_read_session and (selected[0] not in {"log", "cat-file", "merge-base", "diff", "diff-tree"}
                    or any(part == "--output" or part.startswith("--output=") for part in selected))):
                raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Only bounded-query Git reads are supported in an active read session")
            command = [git, "--no-pager", "--no-lazy-fetch", "--no-replace-objects", *selected]
            # Internal safety overrides follow caller identity but precede subcommand.
            offset = 4
            while offset < len(command) and command[offset] == "-c":
                offset += 2
            command[offset:offset] = [part for k, v in settings for part in ("-c", f"{k}={v}")]
            if os.name != 'nt':
                from .git_linux import writable_git_paths
                if selected[:2] == ['worktree', 'prune']:
                    pins.worktree_views(common)
                command = pins.wrap(command, cwd=cwd,
                    writable=[] if _read_session else writable_git_paths(cwd, gitdir, common, selected))
            return command
        try:
            yield (command_for if _read_session else command_for(None)), env
        finally:
            active = False


@contextmanager
def protected_git_read_session(cwd):
    """Reuse pinned configuration for sequential, trusted read-only plumbing.

    Callers retain their existing path/task authorization and output budgets.
    Every generated command passes the normal argument guard and Linux wrapper;
    a returned builder expires before the configuration pins are released.
    """
    with protected_git(cwd, ["git", "cat-file", "--batch"], _read_session=True) as session:
        yield session


@contextmanager
def protected_git_config(cwd, args):
    """Only non-executing init/remote configuration builtins may write config.

    Configuration itself cannot be held against replacement while intentionally
    updating it. Pin repository identity, use the fixed executable/environment,
    and never let this narrow data path invoke remote update or arbitrary Git.
    """
    body = list(args[1:])
    valid = body in (["init"], ["--help"], ["--version"]) or (len(body) >= 3 and body[0] == "remote" and
        body[1] in {"add", "remove", "set-url", "get-url"} and
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", body[2]) and
        len(body) == (4 if body[1] in {"add", "set-url"} else 3))
    if not args or args[0] != "git" or not valid or (os.name != "nt" and sys.platform != 'linux'):
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Unsupported managed Git configuration operation")
    cwd = Path(cwd).absolute()
    git, env = _executable(cwd), _environment()
    with ExitStack() as owners:
        scratch = Path(owners.enter_context(tempfile.TemporaryDirectory(prefix='gitgo-git-config-')))
        if os.name == 'nt':
            pins = _WindowsPins(owners)
        else:
            from .git_linux import LinuxPins
            pins = LinuxPins(owners, scratch)
        pins.directory(cwd)
        if body[0] not in {"init", "--help", "--version"}:
            gitdir, common = _metadata(cwd, pins, protect_config=False)
            from backend.core.sandbox_read import ReadRoot
            with ReadRoot(common) as root:
                with root.open("config") as opened:
                    opened.read_bytes(max_bytes=_MAX_CONFIG)
            env.update(GIT_DIR=str(gitdir), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(cwd))
        command = [git, "--no-pager", "-c", "core.fsmonitor=false", *body]
        if os.name != 'nt':
            # This narrow builtin intentionally changes configuration. It does
            # not expose a snapshot as a writable target or execute extensions.
            command = pins.wrap(command, cwd=cwd, writable=[cwd, *([gitdir, common] if body[0] not in {'init', '--help', '--version'} else [])])
        yield command, env
