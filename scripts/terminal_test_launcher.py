"""Repeatable, human-operated formal terminal selection with owned cleanup.

The coordinator owns the first console; a detached keeper owns the temporary
profile across terminal handoffs. No injected keystrokes, terminal paths, or
model calls. Windows process handles distinguish process exit from PID reuse.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / ".gitgo" / "terminal-tests"
RUN_NAME = re.compile(r"run-[0-9a-f]{32}\Z")


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class WindowsProcess:
    """Read-only process identity and lifetime; never signal or kill a PID."""

    def __init__(self, pid: int):
        if os.name != "nt":
            raise RuntimeError("The terminal BAT test requires Windows")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.handle = self.api.OpenProcess(0x00100000 | 0x1000, False, pid)
        if not self.handle:
            error = ctypes.get_last_error()
            if error == 87:  # process already exited, not access denied
                raise ProcessLookupError(pid)
            raise OSError(error, f"Cannot inspect process {pid}")
        self.pid = pid
        try:
            if not self.alive():
                raise ProcessLookupError(pid)
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if not self.api.QueryFullProcessImageNameW(self.handle, 0, name, ctypes.byref(size)):
                error = ctypes.get_last_error()
                # A process may exit after OpenProcess. Only a signaled handle
                # permits treating a failed identity query as an exited process.
                if not self.alive():
                    raise ProcessLookupError(pid)
                raise ctypes.WinError(error)
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.api.GetProcessTimes(self.handle, *(ctypes.byref(item) for item in times)):
                error = ctypes.get_last_error()
                if not self.alive():
                    raise ProcessLookupError(pid)
                raise ctypes.WinError(error)
            self.executable = str(Path(name.value).resolve())
            self.started_at = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 10000 - 11644473600000
        except BaseException:
            self.close()
            raise

    def identity(self) -> dict:
        return {"pid": self.pid, "executable": self.executable, "startedAt": self.started_at}

    def alive(self) -> bool:
        result = self.api.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def matches(process: WindowsProcess, record: dict, *, tolerance_ms: float = 1) -> bool:
    return (os.path.normcase(process.executable) == os.path.normcase(str(Path(record["executable"]).resolve()))
            and abs(process.started_at - float(record["startedAt"])) <= tolerance_ms)


def owned_session(root: Path, token: str, *, base: Path = BASE) -> dict:
    # Only direct UUID children of the fixed namespace can ever be deleted.
    # resolve() rejects a replaced root junction pointing elsewhere.
    base = base.resolve()
    absolute = root.absolute()
    if absolute.parent != base or not RUN_NAME.fullmatch(absolute.name) or root.resolve() != absolute:
        raise RuntimeError("Refusing cleanup outside the owned terminal-test directory")
    session = json.loads((root / "session.json").read_text(encoding="utf-8"))
    if session.get("version") != 1 or session.get("token") != token or Path(session.get("root", "")).resolve() != absolute:
        raise RuntimeError("Refusing cleanup without a matching ownership marker")
    if (root / "participants").resolve() != root / "participants":
        raise RuntimeError("Refusing a redirected participant directory")
    return session


def cleanup(root: Path, token: str, *, base: Path = BASE, retry_seconds: float = 20) -> None:
    owned_session(root, token, base=base)
    deadline = time.monotonic() + retry_seconds
    while True:
        # A partially successful rmtree may already have removed the marker.
        # Recheck the absolute boundary/junction on every retry, using the
        # ownership established before the first deletion.
        if root.absolute().parent != base.resolve() or root.resolve() != root.absolute():
            raise RuntimeError("Cleanup target changed; refusing deletion")
        try:
            # Python 3.12 on Windows removes junctions without traversing them.
            shutil.rmtree(root)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def prepare(*, base: Path = BASE, bun: Path | None = None) -> tuple[Path, dict]:
    sys.path.insert(0, str(ROOT))
    from backend.core.config import ConfigManager
    from backend.core.llm_config import LLMConfigManager

    bun = (bun or Path(os.environ.get("GITGO_BUN") or Path.home() / ".bun/bun.exe")).resolve()
    if not bun.is_file():
        raise RuntimeError(f"Bun not found: {bun}. Set GITGO_BUN to bun.exe.")
    base = base.absolute()
    base.mkdir(parents=True, exist_ok=True)
    if base.resolve() != base:
        raise RuntimeError("Terminal-test namespace must not be a junction or symlink")
    owner = WindowsProcess(os.getpid())
    try:
        root = base / f"run-{uuid.uuid4().hex}"
        root.mkdir()  # never reuse a previous profile
        token = str(uuid.uuid4())
        session = {"version": 1, "root": str(root), "token": token,
                   "executable": str(bun), "owner": owner.identity()}
    finally:
        owner.close()
    (root / "participants").mkdir()
    write_json(root / "session.json", session)
    try:
        # Explicit load avoids migrating or writing the real user config.
        original = ConfigManager.find_config() or ConfigManager.default_path()
        config = ConfigManager.load(original, strict=True)
        config.launcher = {"terminal": "auto", "command": "", "args": [], "configured": False}
        ConfigManager.save(config, root / "config.json", allow_empty=True)
        for source, target in [(LLMConfigManager._config_path(), "llm_config.json"),
                               (LLMConfigManager._secret_path(), "provider_secrets.json")]:
            if source.is_file():
                shutil.copyfile(source, root / target)
        return root, session
    except BaseException:
        cleanup(root, token, base=base)
        raise


def test_environment(root: Path, session: dict) -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GITGO_CONFIG_PATH": str(root / "config.json"), "GITGO_STATE_HOME": str(root / "state"),
                "GITGO_LLM_CONFIG_PATH": str(root / "llm_config.json"),
                "GITGO_LLM_SECRET_PATH": str(root / "provider_secrets.json"),
                "GITGO_LAUNCH_SESSION": str(root / "session.json"), "GITGO_LAUNCH_SESSION_TOKEN": session["token"],
                "GITGO_PYTHON": sys.executable, "GITGO_BUN": session["executable"], "PYTHONUTF8": "1"})
    # Source acceptance must use the source Host, even from a packaged environment.
    env.pop("GITGO_INSTALL_ROOT", None)
    env.pop("GITGO_HOST_EXECUTABLE", None)
    return env


def keep(root: Path, token: str, *, base: Path = BASE) -> int:
    handles: list[WindowsProcess] = []
    seen: set[Path] = set()
    warnings: list[str] = []
    try:
        session = owned_session(root, token, base=base)
        try:
            owner = WindowsProcess(session["owner"]["pid"])
        except ProcessLookupError:
            owner = None
        if owner:
            handles.append(owner)
            if not matches(owner, session["owner"]):
                raise RuntimeError("Coordinator identity changed; refusing cleanup")
        write_json(root / "keeper-ready.json", {"token": token, "pid": os.getpid()})
        quiet_since = None
        while True:
            for path in (root / "participants").glob("*.json"):
                if path in seen:
                    continue
                record = json.loads(path.read_text(encoding="utf-8"))
                seen.add(path)
                if record.get("token") != token or not isinstance(record.get("pid"), int) or record["pid"] <= 0:
                    raise RuntimeError("Invalid participant record; refusing cleanup")
                try:
                    participant = WindowsProcess(record["pid"])
                except ProcessLookupError:
                    continue
                # Bun's process.uptime() has a small startup offset. Bind both
                # image and birth time; once opened the handle cannot be reused.
                if not matches(participant, record, tolerance_ms=10000) or os.path.normcase(participant.executable) != os.path.normcase(session["executable"]):
                    participant.close()
                    raise RuntimeError("Participant identity mismatch; refusing cleanup")
                handles.append(participant)
            if any(handle.alive() for handle in handles):
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= 1:
                break
            time.sleep(0.2)
        selected = json.loads((root / "config.json").read_text(encoding="utf-8")).get("launcher", {}).get("terminal")
        cleanup(root, token, base=base)
        write_json(base / "last-result.json", {"run": root.name, "cleaned": True, "selected_terminal": selected,
                   "participants": len(seen), "visible_ui_verified": False,
                   "note": "Lifecycle result only; terminal appearance is verified by the human."})
        return 0
    except Exception as error:
        warnings.append(str(error))
        write_json(base / f"{root.name}.cleanup-error.json", {"run": root.name, "cleaned": False, "errors": warnings})
        return 1
    finally:
        for handle in handles:
            handle.close()


def launch() -> int:
    if os.name != "nt":
        raise RuntimeError("The terminal BAT test requires Windows")
    if BASE.exists():
        for report in BASE.glob("run-*.cleanup-error.json"):
            print(f"[gitgo-test] Previous cleanup failed; details: {report}", flush=True)
    root, session = prepare()
    keeper = None
    try:
        keeper = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--keep", str(root), session["token"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, cwd=ROOT)
        deadline = time.monotonic() + 10
        while not (root / "keeper-ready.json").exists():
            if keeper.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"Cleanup keeper did not start. See {BASE}.")
            time.sleep(0.1)
        print("[gitgo-test] Temporary profile. Select with Left/Right, Enter to save and open the terminal.", flush=True)
        print("[gitgo-test] Close the test Gitgo to clean up. Every click starts a fresh selector.", flush=True)
        print(f"[gitgo-test] Profile: {root}", flush=True)
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        buffer = ctypes.create_unicode_buffer(32768)
        if not api.GetSystemDirectoryW(buffer, len(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
        cmd = Path(buffer.value) / "cmd.exe"
        # Relative trusted BAT, fixed cwd, inherited real console and normal UI.
        return subprocess.call([str(cmd), "/d", "/c", "run_dashboard_native.bat"], cwd=ROOT,
                               env=test_environment(root, session))
    except BaseException:
        if keeper is None or keeper.poll() is not None:
            cleanup(root, session["token"])
        raise
    # On handoff the coordinator exits, but the detached keeper follows the
    # selected dashboard's registered OS handle until that dashboard exits.


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", nargs=2, metavar=("ROOT", "TOKEN"))
    args = parser.parse_args()
    try:
        sys.exit(keep(Path(args.keep[0]), args.keep[1]) if args.keep else launch())
    except Exception as error:
        print(f"[gitgo-test] {error}", file=sys.stderr, flush=True)
        sys.exit(1)
