"""Debug launcher — 直接运行 GUI，崩溃时保持控制台不关闭（Python异常级别）。

用法: python debug_entry.py

C++ segfault 级别的崩溃无法被 Python 捕获。为此 build.py --debug 会
同时生成 run_debug.bat，双击 .bat 即可在 segfault 时也保持控制台存活。
"""

import sys
import os
import time
import traceback
from pathlib import Path

# 确保项目在 sys.path
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

# PyInstaller 兼容
_meipass = getattr(sys, "_MEIPASS", None)
if _meipass and _meipass not in sys.path:
    sys.path.insert(0, _meipass)


def main():
    print(f"╔══════════════════════════════════════════╗")
    print(f"║  gitgo 调试版                            ║")
    print(f"║  Python 异常会显示在此窗口               ║")
    print(f"║  如遇 C++ segfault: 请用 run_debug.bat   ║")
    print(f"╚══════════════════════════════════════════╝")
    print(f"[DEBUG] 启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[DEBUG] Python: {sys.executable}")
    print("-" * 50)
    sys.stdout.flush()

    try:
        from gui_main import entry as gui_entry
        gui_entry()
    except Exception:
        print("\n" + "=" * 50)
        print("[FATAL] 未捕获的 Python 异常:")
        traceback.print_exc()
        print("=" * 50)
    except BaseException:
        print("\n" + "=" * 50)
        print("[FATAL] 系统级异常 (可能是 KeyboardInterrupt 或崩溃)")
        traceback.print_exc()
        print("=" * 50)

    print()
    print("-" * 50)
    print(f"[DEBUG] 程序已退出，时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    input("按 Enter 键关闭此窗口...")


if __name__ == "__main__":
    main()
