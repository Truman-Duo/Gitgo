"""Host-owned native sandbox policy. Approval never disables OS isolation."""
from __future__ import annotations

import hashlib
import os
import shutil
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
    if sys.platform == "win32":
        from backend.core.sandbox_windows import WindowsSandboxProcess
        return WindowsSandboxProcess(argv, policy=policy, **kwargs)
    if sys.platform == "linux":
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise SandboxDenied("SANDBOX_UNAVAILABLE", "Install bubblewrap; no unsandboxed fallback is permitted.")
        # Empty root, private PID/network/user namespaces and private tmpfs.
        # Mount runtime trees read-only, never the host home, /etc or /run.
        command = [bwrap, "--die-with-parent", "--new-session", "--unshare-all",
                   "--cap-drop", "ALL", "--proc", "/proc", "--dev", "/dev",
                   "--tmpfs", "/tmp"]
        for name in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(name).exists():
                command += ["--ro-bind", name, name]
        command += ["--dir", "/etc"]
        for name in ("/etc/ld.so.cache", "/etc/localtime"):
            if Path(name).is_file():
                command += ["--ro-bind", name, name]
        source_root = Path(__file__).resolve().parents[2]
        for root in dict.fromkeys((source_root, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())):
            if not any(root.is_relative_to(Path(tree)) for tree in ("/usr", "/bin", "/lib", "/lib64")):
                command += ["--ro-bind", str(root), str(root)]
        command += ["--bind", str(policy.workspace), str(policy.workspace),
                    "--chdir", str(kwargs.pop("cwd")), "--"]
        # Set limits inside the new PID namespace before loading tool code.
        bootstrap = (
            "import os,resource,sys;"
            f"resource.setrlimit(resource.RLIMIT_AS,({policy.memory_bytes},{policy.memory_bytes}));"
            f"resource.setrlimit(resource.RLIMIT_NPROC,({policy.process_limit},{policy.process_limit}));"
            f"resource.setrlimit(resource.RLIMIT_CPU,({policy.cpu_seconds},{policy.cpu_seconds}));"
            "resource.setrlimit(resource.RLIMIT_CORE,(0,0));"
            "os.execv(sys.argv[1],sys.argv[1:])"
        )
        # frozen runtimes have no general -c mode; refuse rather than weaken.
        if getattr(sys, "frozen", False):
            raise SandboxDenied("SANDBOX_UNAVAILABLE", "Linux frozen runtime sandbox bootstrap is unavailable.")
        command += [sys.executable, "-I", "-c", bootstrap, *argv]
        return subprocess.Popen(command, **kwargs)
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
