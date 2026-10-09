"""Host-owned native sandbox policy. Approval never disables OS isolation."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# These handlers can execute arbitrary code, including registration tests.
SANDBOXED_HANDLERS = frozenset({
    "exec_command", "shell_script", "run_command", "run_test",
    "authored_python", "authored_privileged_python", "dynamic_composite",
    "apply_patch", "search_text", "git_status", "git_diff", "git_log", "git_branch",
    "formalize",
})


class SandboxDenied(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code

    def result(self) -> dict:
        from backend.core.errors import error_payload
        return {
            **error_payload(self.code, message=str(self), next_actions=[
                {"action": "configure_native_sandbox",
                 "effect": "Provision the documented runtime/workspace sandbox access; never retry unsandboxed."},
                {"action": "request_permission",
                 "effect": "Request an exact tool/resource grant if additional access is needed. Approval does not disable isolation."},
            ]),
            "effect_state": getattr(self, "effect_state", "not_committed"),
        }



def trusted_runtime_roots() -> tuple[Path, ...]:
    """Host-selected code and Python roots, never a model-supplied allowlist."""
    roots = [Path(__file__).resolve().parents[2], Path(sys.base_prefix), Path(sys.prefix)]
    if getattr(sys, "frozen", False):
        roots += [Path(sys.executable).parent, Path(getattr(sys, "_MEIPASS", sys.prefix))]
    try:
        return tuple(dict.fromkeys(root.resolve(strict=True) for root in roots))
    except (OSError, RuntimeError) as exc:
        raise SandboxDenied("SANDBOX_POLICY_INVALID", "Trusted runtime paths are unavailable.") from exc


def validate_runtime_separation(workspace: Path, roots) -> None:
    """A writable workspace cannot contain or sit inside trusted runtime roots."""
    try:
        workspace = workspace.resolve(strict=True)
        for raw in roots:
            root = Path(raw).resolve(strict=True)
            if workspace.is_relative_to(root) or root.is_relative_to(workspace):
                raise SandboxDenied("SANDBOX_POLICY_INVALID",
                    "The writable workspace overlaps trusted runtime code. "
                    "Use a separate runtime installation outside the execution workspace.")
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, SandboxDenied):
            raise
        raise SandboxDenied("SANDBOX_POLICY_INVALID", "Trusted runtime paths are unavailable.") from exc


@dataclass(frozen=True)
class SandboxPolicy:
    workspace: Path
    memory_bytes: int = 512 * 1024 * 1024
    process_limit: int = 32
    cpu_seconds: int = 120

    def __post_init__(self):
        try:
            root = self.workspace.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "The execution workspace is unavailable.") from exc
        if not root.is_dir() or root == Path(root.anchor):
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "A concrete workspace directory is required.")
        validate_runtime_separation(root, trusted_runtime_roots())
        object.__setattr__(self, "workspace", root)
        if min(self.memory_bytes, self.process_limit, self.cpu_seconds) <= 0:
            raise SandboxDenied("SANDBOX_POLICY_INVALID", "Sandbox resource limits must be positive.")

    @property
    def profile_name(self) -> str:
        digest = hashlib.sha256(os.path.normcase(str(self.workspace)).encode()).hexdigest()[:32]
        return "Gitgo.Workspace." + digest


def sandbox_environment(source: dict[str, str]) -> dict[str, str]:
    # Allowlist instead of guessing every possible credential variable name.
    allowed = {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT",
        "LANG", "LC_ALL", "TZ", "PYTHONIOENCODING", "PYTHONUTF8", "LOCALAPPDATA",
    }
    env = {k: v for k, v in source.items() if k.upper() in allowed}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1", GITGO_AGENT_TOOL="1")
    return env


def sandbox_popen(argv: list[str], policy: SandboxPolicy, **kwargs):
    validate_runtime_separation(policy.workspace, trusted_runtime_roots())
    if sys.platform == "win32":
        from backend.core.sandbox_windows import WindowsSandboxProcess
        return WindowsSandboxProcess(argv, policy=policy, **kwargs)
    if sys.platform == "linux":
        from backend.core.sandbox_linux import LinuxCgroup, LinuxSandboxProcess, trusted_bwrap
        if kwargs.get('pass_fds') or kwargs.get('close_fds') is False:
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Sandbox children cannot inherit Host descriptors.')
        if any(kwargs.get(stream) != subprocess.PIPE for stream in ('stdin', 'stdout', 'stderr')):
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Sandbox requires private stdio pipes.')
        bwrap = trusted_bwrap(policy.workspace)
        from backend.core.child_process import python_command
        # Empty root, private PID/network/user namespaces and private tmpfs.
        # Mount runtime trees read-only, never the host home, /etc or /run.
        command = [bwrap, "--die-with-parent", "--new-session",
                   "--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
                   "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev",
                   "--tmpfs", "/tmp"]
        for name in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(name).exists():
                command += ["--ro-bind", name, name]
        command += ["--dir", "/etc"]
        for name in ("/etc/ld.so.cache", "/etc/localtime"):
            if Path(name).is_file():
                command += ["--ro-bind", name, name]
        for root in trusted_runtime_roots():
            if not any(root.is_relative_to(Path(tree)) for tree in ("/usr", "/bin", "/lib", "/lib64")):
                command += ["--ro-bind", str(root), str(root)]
        cgroup = LinuxCgroup(policy)
        command += ["--dir", "/run",
                    "--bind", str(cgroup.path / "cgroup.procs"), "/run/gitgo-cgroup.procs",
                    "--bind", str(policy.workspace), str(policy.workspace),
                    "--chdir", str(kwargs.pop("cwd")), "--"]
        # Set limits inside the new PID namespace before loading tool code.
        bootstrap = (
            "import os,resource,sys;"
            # This is the only cgroup file exposed: it can move a visible
            # process INTO this invocation, never change limits or move out.
            "f=os.open('/run/gitgo-cgroup.procs',os.O_WRONLY);os.write(f,b'0');os.close(f);"
            f"resource.setrlimit(resource.RLIMIT_AS,({policy.memory_bytes},{policy.memory_bytes}));"
            f"resource.setrlimit(resource.RLIMIT_CPU,({policy.cpu_seconds},{policy.cpu_seconds}));"
            "resource.setrlimit(resource.RLIMIT_CORE,(0,0));"
            "os.execv(sys.argv[1],sys.argv[1:])"
        )
        # The packaged Host exposes a private Python role; do not treat its
        # executable as a general system interpreter.
        command += python_command(["-I", "-c", bootstrap, *argv])
        from backend.core.sandbox_seccomp import socket_filter
        try:
            with socket_filter(policy.workspace) as program:
                # bubblewrap consumes/closes this descriptor before tool exec.
                # No Host descriptor other than the three stdio pipes survives.
                command[1:1] = ['--seccomp', str(program.fileno())]
                kwargs.update(pass_fds=(program.fileno(),), close_fds=True)
                return LinuxSandboxProcess(command, cgroup=cgroup, cpu_seconds=policy.cpu_seconds, **kwargs)
        except BaseException:
            cgroup.close()
            raise
    raise SandboxDenied("SANDBOX_UNAVAILABLE", "No native sandbox backend is available on this platform.")

def prepare_child_environment(workspace: str) -> None:
    """Put shell/profile/cache/temp writes inside the already isolated workspace.

    The outer Windows CreateProcess needs the real LOCALAPPDATA to locate its
    AppContainer profile. Rebind user folders only after entering the container.
    """
    # Host already canonicalized this path before launch. Strict resolution
    # here would probe ancestors that AppContainer intentionally cannot read.
    root = Path(workspace)
    home = (root / ".gitgo" / "sandbox" / "home").resolve(strict=False)
    if not home.is_relative_to(root):
        raise SandboxDenied("SANDBOX_POLICY_INVALID", "Sandbox home escapes the workspace.")
    folders = {"USERPROFILE": home, "HOME": home, "APPDATA": home / "Roaming",
               "LOCALAPPDATA": home / "Local", "TEMP": home / "Temp",
               "TMP": home / "Temp", "TMPDIR": home / "Temp"}
    for folder in set(folders.values()):
        folder.mkdir(parents=True, exist_ok=True)
    os.environ.update({key: str(value) for key, value in folders.items()})
    os.environ["PSModuleAnalysisCachePath"] = str(home / "Local" / "ModuleAnalysisCache")
