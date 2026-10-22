"""Managed Git transport configuration, separate from model execution authority.

Repository programs cannot run in the trusted transfer process. Operator-wide
SSH/credential configuration is kept in memory, never copied into argv or a
persistent receipt. This module grants neither network nor publication approval.
"""
from pathlib import Path
import os
import re
import shlex
from urllib.parse import unquote, urlsplit

from backend.core.loop.git_execution import GitBoundaryError, _invoke, _entries, _read_config, _metadata, _MAX_CONFIG

TRANSPORT_COMMANDS = frozenset({"fetch", "push"})
_PROTOCOLS = frozenset({"http", "https", "git", "ssh", "file"})
_CLIENT_HOOKS = {"fetch": ("reference-transaction",), "push": ("pre-push", "reference-transaction")}
_RECEIVE_HOOKS = ("pre-receive", "update", "post-receive", "post-update", "push-to-checkout", "proc-receive", "reference-transaction")


def _deny(message):
    raise GitBoundaryError("GIT_REPOSITORY_EXTENSION_UNSUPPORTED", message)


def _reject_programs(entries, *, protocols, command, remote=None, server=False):
    for key, value in entries:
        key = key.casefold()
        if not value:
            continue
        forbidden = key == "core.hookspath" or key == "core.alternaterefscommand"
        if server:
            forbidden |= command == "fetch" and key == "uploadpack.packobjectshook"
        else:
            forbidden |= "ssh" in protocols and key == "core.sshcommand"
            forbidden |= "git" in protocols and key == "core.gitproxy"
            forbidden |= bool(protocols & {"http", "https"}) and (
                key == "core.askpass" or key.startswith("credential.") and key.endswith(".helper"))
            if remote:
                prefix = "remote." + remote.casefold() + "."
                forbidden |= key == prefix + "vcs"
                forbidden |= key == prefix + ("uploadpack" if command == "fetch" else "receivepack")
        if forbidden:
            _deny("Repository executable extensions are unsupported for managed Git transfer; use operator-wide configuration instead")


def _reject_hooks(common, pins, names):
    hooks = common / "hooks"
    if not os.path.lexists(hooks):
        return
    pins.directory(hooks)
    if any(os.path.lexists(hooks / name) for name in names):
        _deny("Repository executable hooks are unsupported for managed Git transfer")


def _url_target(value, cwd):
    if "::" in value:
        _deny("Custom Git remote helpers are unsupported")
    if re.match(r"^[A-Za-z]:[/\\]", value):
        return "file", Path(value)
    if "://" in value:
        parsed = urlsplit(value)
        protocol = parsed.scheme.casefold()
        if protocol not in _PROTOCOLS:
            _deny("Only standard Git transports are supported")
        if protocol != "file":
            return protocol, None
        path = unquote(parsed.path)
        if parsed.netloc and parsed.netloc.casefold() != "localhost":
            path = "//" + parsed.netloc + path
        elif os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return "file", Path(path)
    if ":" in value:
        return "ssh", None
    path = Path(value)
    return "file", path if path.is_absolute() else cwd / path


def _operator_entries(git, cwd, env, pins, forbidden_roots):
    observed = _invoke([git, "config", "--null", "--show-scope", "--show-origin", "--list"],
                       cwd=cwd, env=env, timeout=15)
    if observed.returncode or len(observed.stdout) > _MAX_CONFIG:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Cannot obtain bounded operator transport configuration")
    fields = observed.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 3:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Unknown scoped Git configuration format")
    records, origins = [], set()
    for index in range(0, len(fields), 3):
        scope, origin, entry = fields[index:index + 3]
        if scope not in {b"global", b"system"}:
            continue
        if not origin.startswith(b"file:"):
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Operator configuration must have a file origin")
        path = Path(os.fsdecode(origin[5:]))
        path = path if path.is_absolute() else cwd / path
        origins.add(Path(os.path.abspath(path)))
        records.extend(_entries(entry + b"\0"))
    # Pin every potential include branch, including currently inactive ones.
    # An operator include inside either repository is not operator authority.
    for origin in sorted(origins):
        _read_config(git, origin, cwd=cwd, env=env, pins=pins,
                     forbidden_roots=forbidden_roots)
    return [(key, value) for key, value in records if
            key.casefold().startswith(("credential.", "http.")) or
            key.casefold() in {"core.sshcommand", "core.gitproxy", "ssh.variant"} or
            key.casefold().startswith("url.") and key.casefold().endswith((".insteadof", ".pushinsteadof"))]


def prepare_transfer(git, cwd, body, *, bootstrap, env, local, entries, pins, gitdir, common):
    """Validate the fixed named transfer and bind trusted transport settings."""
    if len(body) != 2 or body[0] not in TRANSPORT_COMMANDS or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", body[1]):
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Managed transfer requires one named remote, without executable/options overrides")
    command, remote = body
    for key, value in local:
        if key.casefold() in {f"remote.{remote.casefold()}.url", f"remote.{remote.casefold()}.pushurl"} and any(c in value for c in "\r\n\0"):
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Remote URL must be a single bounded value")
    args = [git, "remote", "get-url", *( ["--push"] if command == "push" else []), "--all", remote]
    observed = _invoke(args, cwd=cwd, env=bootstrap, timeout=15)
    if observed.returncode:
        raise GitBoundaryError("GIT_REMOTE_UNAVAILABLE", "Named Git remote is unavailable")
    if len(observed.stdout) > 512 * 1024:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Remote URL inventory exceeds its bound")
    try:
        urls = observed.stdout.decode("utf8").splitlines()
    except UnicodeError as exc:
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Remote URL is not UTF-8") from exc
    if not urls or len(urls) > 64 or any(not url or len(url) > 8192 for url in urls):
        raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Remote URL inventory is invalid")
    targets = [_url_target(url, cwd) for url in urls]
    protocols = {protocol for protocol, _ in targets}
    if "file" in protocols and len(protocols) != 1:
        _deny("A managed remote cannot mix local and network transport targets")
    _reject_programs(local, protocols=protocols, command=command, remote=remote)
    _reject_hooks(common, pins, _CLIENT_HOOKS[command])
    pins.file(gitdir / "HEAD")
    roots = [cwd, common]
    if common.name.casefold() == ".git":
        roots.append(common.parent)
    for _, target in targets:
        if target is None:
            continue
        target = Path(os.path.abspath(target))
        if not target.exists():
            raise GitBoundaryError("GIT_REMOTE_UNAVAILABLE", "Local Git remote does not exist")
        # Bare receive/upload repositories and ordinary local worktrees use the
        # same configuration object protections; never erase reparse identities.
        if os.path.lexists(target / ".git"):
            target_gitdir, target_common = _metadata(target, pins)
        else:
            pins.directory(target)
            pins.file(target / "HEAD")
            pins.file(target / "config")
            target_gitdir = target_common = target
        graph = _read_config(git, target_common / "config", cwd=target, env=bootstrap, pins=pins)
        if any(key.casefold() == "extensions.worktreeconfig" and value.casefold() not in {"false", "no", "off", "0"} for key, value in graph):
            _deny("Local transfer targets with per-worktree configuration are not supported")
        if command == "push" and any(key.casefold() == "receive.denycurrentbranch" and value.casefold() == "updateinstead" for key, value in graph):
            _deny("Managed push does not execute checkout updates in a local receiver")
        _reject_programs(graph, protocols={"file"}, command=command, server=True)
        _reject_hooks(target_common, pins, _RECEIVE_HOOKS if command == "push" else ())
        roots.extend((target, target_common))
        if target_common.name.casefold() == ".git":
            roots.append(target_common.parent)
    operator = _operator_entries(git, cwd, bootstrap, pins, tuple(roots))
    # Git's environment config is below CLI safety overrides. Values, including
    # operator credential helpers/headers, never enter logged command arguments.
    # Checking hook existence cannot prevent a new hook after preflight.
    # Git clears environment config for its local upload/receive child, so bind
    # that child through a fixed internal Git command as well (below).
    inherited_safety = [("core.hooksPath", str(Path(env["GIT_CONFIG_GLOBAL"]).parent / "hooks")),
        ("core.fsmonitor", "false"), ("core.alternateRefsCommand", ""),
        ("uploadpack.packObjectsHook", ""), ("receive.denyCurrentBranch", "refuse"),
        ("gc.auto", "0"), ("maintenance.auto", "false")]
    operator.extend(inherited_safety)
    env["GIT_CONFIG_COUNT"] = str(len(operator))
    for index, (key, value) in enumerate(operator):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_ASKPASS"] = ""
    settings = [("protocol.allow", "never"), ("push.gpgSign", "false"),
                ("core.askPass", ""), ("core.alternateRefsCommand", ""),
                ("uploadpack.packObjectsHook", ""), ("fetch.recurseSubmodules", "false")]
    for key, _ in [*local, *entries]:
        if re.fullmatch(r"protocol\..+\.allow", key.casefold()):
            settings.append((key, "never"))
    settings.extend((f"protocol.{protocol}.allow", "always") for protocol in sorted(_PROTOCOLS))
    if protocols == {"file"}:
        # This is a trusted Git builtin, not a repository-provided program. Git
        # itself appends/quotes the target path. Never apply a local executable
        # path to an SSH server, whose executable namespace is different.
        child = [git.replace("\\", "/"), "--no-pager"]
        child.extend(part for key, value in inherited_safety for part in ("-c", f"{key}={value}"))
        child.append("receive-pack" if command == "push" else "upload-pack")
        option = "--receive-pack=" if command == "push" else "--upload-pack="
        body.insert(1, option + " ".join(shlex.quote(part) for part in child))
    return settings
