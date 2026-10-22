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
import tempfile


class GitBoundaryError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


CONTENT_READ_COMMANDS = frozenset({"rev-parse", "diff", "ls-files", "cat-file", "show", "ls-tree", "log",
                                   "status", "rev-list", "for-each-ref", "merge-base", "grep", "diff-tree"})
LOCAL_WRITE_COMMANDS = frozenset({"apply", "add", "reset", "commit", "update-ref", "hash-object", "rm", "branch", "cherry-pick"})
_ALLOWED = CONTENT_READ_COMMANDS | LOCAL_WRITE_COMMANDS | {"worktree", "fetch", "push"}
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
    result = _invoke([git, "config", "--file", str(path), "--no-includes",
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
    options = body[index + 1:]
    if "--" in options:
        options = options[:options.index("--")]
    if any(part in {"--ext-diff", "--textconv"} or part.startswith(("--ext-diff=", "--textconv=")) for part in options):
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Executable diff transformations are outside managed Git reads")
    if body[index] in {"diff", "show", "log", "diff-tree"}:
        body[index + 1:index + 1] = ["--no-ext-diff", "--no-textconv"]
    return body


@contextmanager
def protected_git(cwd, args):
    """Yield a fixed command/environment while configuration objects are pinned.

    The service may still perform its intended filesystem effects. This closes
    executable Git extensions, not arbitrary filesystem races or semantic review.
    """
    body = _arguments(args)
    if os.name != "nt":
        raise GitBoundaryError("GIT_BOUNDARY_UNAVAILABLE", "protected trusted Git currently requires Windows; no unrestricted fallback")
    cwd = Path(cwd).absolute()
    git = _executable(cwd)
    with ExitStack() as owners:
        pins = _WindowsPins(owners)
        gitdir, common = _metadata(cwd, pins)
        scratch = Path(owners.enter_context(tempfile.TemporaryDirectory(prefix="gitgo-git-")))
        if scratch.is_relative_to(cwd) or scratch.is_relative_to(common):
            raise GitBoundaryError("GIT_BOUNDARY_UNAVAILABLE", "private scratch must be outside the execution and repository metadata trees")
        env = _environment()
        env.update(GIT_DIR=str(gitdir), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(cwd))
        # The config builtin only parses data; it never runs filters/hooks.
        # First pin the repository include graph, then capture effective
        # operator preferences/driver names. No raw credentials are persisted.
        local = _read_config(git, common / "config", cwd=scratch, env=env, pins=pins)
        if any(k == "extensions.worktreeconfig" for k, _ in local):
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
                local=local, entries=entries, pins=pins, gitdir=gitdir, common=common))
        if body[0] == "cherry-pick" and any(re.fullmatch(r"merge\..+\.driver", key) and value for key, value in [*entries, *local]):
            raise GitBoundaryError("GIT_REPOSITORY_EXTENSION_UNSUPPORTED", "Custom merge programs are unsupported for managed cherry-pick")
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
        command = [git, "--no-pager", "--no-lazy-fetch", "--no-replace-objects", *body]
        # Internal safety overrides follow caller identity but precede subcommand.
        offset = 4
        while offset < len(command) and command[offset] == "-c":
            offset += 2
        command[offset:offset] = [part for k, v in settings for part in ("-c", f"{k}={v}")]
        yield command, env


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
    if not args or args[0] != "git" or not valid or os.name != "nt":
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Unsupported managed Git configuration operation")
    cwd = Path(cwd).absolute()
    git, env = _executable(cwd), _environment()
    with ExitStack() as owners:
        pins = _WindowsPins(owners)
        pins.directory(cwd)
        if body[0] not in {"init", "--help", "--version"}:
            gitdir, common = _metadata(cwd, pins, protect_config=False)
            from backend.core.sandbox_read import ReadRoot
            with ReadRoot(common) as root:
                with root.open("config") as opened:
                    opened.read_bytes(max_bytes=_MAX_CONFIG)
            env.update(GIT_DIR=str(gitdir), GIT_COMMON_DIR=str(common), GIT_WORK_TREE=str(cwd))
        yield [git, "--no-pager", "-c", "core.fsmonitor=false", *body], env
