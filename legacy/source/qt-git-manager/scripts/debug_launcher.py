"""Debug Launcher — 父进程启动子进程 gitgo_core.exe，崩溃时保活。

用法: 由 build.py --debug 打包为 gitgo_debug.exe
     自动查找同目录的 gitgo_core.exe 并启动为子进程
"""

import subprocess
import sys
import os
import time
from pathlib import Path


def main():
    # 确定子进程 exe 路径（同目录 gitgo_core.exe）
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent / "dist"
    core_exe = exe_dir / "gitgo_core.exe"

    if not core_exe.exists():
        # 开发模式：直接在同目录找
        core_exe = Path(sys.executable).parent / "gitgo_core.exe" if getattr(sys, "frozen", False) else Path.cwd() / "dist" / "gitgo_core.exe"

    if not core_exe.exists():
        print(f"[LAUNCHER] ERROR: 找不到 gitgo_core.exe")
        print(f"  搜索路径: {core_exe}")
        input("按 Enter 关闭...")
        sys.exit(1)

    print("=" * 52)
    print("  gitgo DEBUG LAUNCHER (Parent Process)")
    print("=" * 52)
    print(f"  Parent PID:  {os.getpid()}")
    print(f"  Child exe:   {core_exe}")
    print(f"  Start time:  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 52)
    print("  [CHILD OUTPUT BELOW]")
    print("-" * 52)
    sys.stdout.flush()

    # 启动子进程，stderr 透传到本控制台
    try:
        proc = subprocess.Popen(
            [str(core_exe)] + sys.argv[1:],
            stderr=subprocess.STDOUT,   # stderr 合并到 stdout
            stdout=sys.stdout,           # 直接透传
            cwd=str(core_exe.parent),
        )
        exit_code = proc.wait()
    except Exception as e:
        print(f"\n[LAUNCHER] 启动子进程失败: {e}")
        exit_code = -1

    print()
    print("-" * 52)
    print(f"  [CHILD EXITED]")
    if exit_code < 0:
        print(f"  Status: CRASHED (signal terminated, exit code: {exit_code})")
        print(f"  C++ segfault 或 access violation — 详见上方输出")
    elif exit_code == 0:
        print(f"  Status: Normal exit (code: 0)")
    else:
        print(f"  Status: Abnormal exit (code: {exit_code})")
    print(f"  End time:    {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 52)

    # 只在非 bat 启动时等待（bat 有自己的 pause）
    if os.environ.get("GITGO_BAT_LAUNCH"):
        sys.exit(exit_code)
    input("按 Enter 关闭此窗口...")


if __name__ == "__main__":
    main()
