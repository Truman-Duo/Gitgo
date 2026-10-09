"""Fail before compilation when the selected build environment lacks Host imports."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if len(sys.argv) > 1 and sys.argv[1]:
    packages = Path(sys.argv[1]).resolve()
    sys.path[:0] = [str(packages), str(packages / "win32"), str(packages / "win32" / "lib")]

from backend.core.native_host import NativeHost
from backend.core.daemon import run_daemon
from backend.core.tools.runner import main

print("Selected build runtime can import Host, Daemon and tool runner")
