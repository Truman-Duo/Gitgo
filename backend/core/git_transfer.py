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

TRANSPORT_COMMANDS = frozenset({"fetch", "push", "ls-remote"})
_PROTOCOLS = frozenset({"http", "https", "git", "ssh", "file"})
_CLIENT_HOOKS = {"fetch": ("reference-transaction",), "push": ("pre-push", "reference-transaction"), "ls-remote": ()}
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
            forbidden |= command in {"fetch", "ls-remote"} and key == "uploadpack.packobjectshook"
        else:
            forbidden |= "ssh" in protocols and key == "core.sshcommand"
            forbidden |= "git" in protocols and key == "core.gitproxy"
            forbidden |= bool(protocols & {"http", "https"}) and (
                key == "core.askpass" or key.startswith("credential.") and key.endswith(".helper"))
            if remote:
                prefix = "remote." + remote.casefold() + "."
                forbidden |= key == prefix + "vcs"
                forbidden |= key == prefix + ("receivepack" if command == "push" else "uploadpack")
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


def prepare_transfer(git, cwd, body, *, bootstrap, env, local, entries, pins, gitdir, common, identity=None):
    """Validate the fixed named transfer and bind trusted transport settings."""
    fixed_local = (len(body) == 7 and body[:5] == ["fetch", "--no-tags", "--no-write-fetch-head",
        "--no-recurse-submodules", "--no-auto-maintenance"]
        and Path(body[5]).is_absolute() and not any(c in body[5] for c in "\r\n\0")
        and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", body[6]) is not None)
    oid = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
    named = lambda value: bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", value))
    def exact_ref(value):
        return bool(re.fullmatch(r"refs/(?:heads|tags|gitgo)/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", value)
            and ".." not in value and "//" not in value
            and all(not part.startswith(".") and not part.endswith((".lock", ".")) for part in value.split("/")))
    fixed_read = (len(body) == 5 and body[:3] == ["ls-remote", "--refs", "--exit-code"]
                  and named(body[3]) and exact_ref(body[4]))
    fixed_fetch = (not fixed_local and len(body) == 7 and body[:5] == ["fetch", "--no-tags", "--no-write-fetch-head",
        "--no-recurse-submodules", "--no-auto-maintenance"] and named(body[5]) and re.fullmatch(oid, body[6]))
    fixed_push = False
    if len(body) == 5 and body[:2] == ["push", "--porcelain"] and named(body[3]):
        lease = re.fullmatch(r"--force-with-lease=(.+):(" + oid + r")?", body[2])
        update = re.fullmatch(r"(" + oid + r")?:(.+)", body[4])
        fixed_push = bool(lease and update and exact_ref(lease[1]) and lease[1] == update[2]
                          and (update[1] or lease[2]))
    exact = fixed_read or fixed_fetch or fixed_push
    ordinary = len(body) == 2 and body[0] in {"fetch", "push"} and named(body[1])
    if not fixed_local and not exact and not ordinary:
        raise GitBoundaryError("GIT_COMMAND_UNSUPPORTED", "Managed transfer requires one named remote, without executable/options overrides")
    command = body[0]
    remote = None if fixed_local else body[5] if fixed_fetch else body[3] if fixed_read or fixed_push else body[1]
    if fixed_local and any(key.casefold().startswith("url.") and key.casefold().endswith((".insteadof", ".pushinsteadof")) for key, _ in [*entries, *local]):
        _deny("Fixed local object import cannot use URL rewrites")
    for key, value in local:
        if remote and key.casefold() in {f"remote.{remote.casefold()}.url", f"remote.{remote.casefold()}.pushurl"} and any(c in value for c in "\r\n\0"):
            raise GitBoundaryError("GIT_CONFIG_UNSUPPORTED", "Remote URL must be a single bounded value")
    if fixed_local:
        urls = [body[5]]
    else:
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
    if exact:
        # One conditional operation has one destination. Reading fetch URL A
        # must never authorize writing push URL B or multiple push targets.
        counterpart = _invoke([git, "remote", "get-url", *([] if command == "push" else ["--push"]), "--all", remote],
                              cwd=cwd, env=bootstrap, timeout=15)
        if counterpart.returncode or len(urls) != 1 or counterpart.stdout.decode("utf8").splitlines() != urls:
            raise GitBoundaryError("GIT_REMOTE_IDENTITY_UNSUPPORTED", "Exact operations require one shared fetch/push destination")
        if "://" in urls[0] and (urlsplit(urls[0]).password or urlsplit(urls[0]).query or urlsplit(urls[0]).fragment):
            raise GitBoundaryError("GIT_REMOTE_IDENTITY_UNSUPPORTED", "Inline credentials or ambiguous URLs are unsupported")
        import hashlib, json
        fingerprint = hashlib.sha256(json.dumps([remote, *urls], ensure_ascii=True).encode()).hexdigest()
        if identity is not None:
            if identity.get("expected") and identity["expected"] != fingerprint:
                raise GitBoundaryError("GIT_REMOTE_CHANGED", "The named remote changed after preparation")
            identity["observed"] = fingerprint
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
        if command == 'push' and hasattr(pins, 'additional_writable'):
            pins.additional_writable.extend([target_gitdir, target_common])
        roots.extend((target, target_common))
        if target_common.name.casefold() == ".git":
            roots.append(target_common.parent)
    operator = [] if fixed_local else _operator_entries(git, cwd, bootstrap, pins, tuple(roots))
    # Git's environment config is below CLI safety overrides. Values, including
    # operator credential helpers/headers, never enter logged command arguments.
    # Checking hook existence cannot prevent a new hook after preflight.
    # Git clears environment config for its local upload/receive child, so bind
    # that child through a fixed internal Git command as well (below).
    inherited_safety = [("core.hooksPath", str(Path(env["GIT_CONFIG_GLOBAL"]).parent / "hooks")),
        ("core.fsmonitor", "false"), ("core.alternateRefsCommand", ""),
        ("receive.denyCurrentBranch", "refuse"),
        ("gc.auto", "0"), ("maintenance.auto", "false")]
    operator.extend(inherited_safety)
    env["GIT_CONFIG_COUNT"] = str(len(operator))
    for index, (key, value) in enumerate(operator):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_ASKPASS"] = ""
    settings = [("protocol.allow", "never"), ("push.gpgSign", "false"),
                ("core.askPass", ""), ("core.alternateRefsCommand", ""),
                ("fetch.recurseSubmodules", "false")]
    if exact:
        settings.extend([("push.followTags", "false"), (f"remote.{remote}.mirror", "false")])
    # An empty packObjectsHook is still an executable hook to Git and breaks
    # transfers that actually need a pack. Local source configuration is pinned
    # and _reject_programs already refuses this hook; operator snapshots never
    # import it. Leave it absent instead of attempting to disable it with "".
    for key, _ in [*local, *entries]:
        if re.fullmatch(r"protocol\..+\.allow", key.casefold()):
            settings.append((key, "never"))
    settings.extend((f"protocol.{protocol}.allow", "always") for protocol in sorted({"file"} if fixed_local else _PROTOCOLS))
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
