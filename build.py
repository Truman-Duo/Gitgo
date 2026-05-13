"""PyInstaller build script for gitgo — 增量构建 + 缓存复用

Usage:
  python build.py                  # Release build (复用缓存)
  python build.py --debug          # Debug build (复用缓存)
  python build.py --release        # Release build (显式)
  python build.py --clean          # 清缓存后全量构建
  python build.py --fast           # 快速模式 (跳过安装+UPX)
  python build.py --skip-install   # 跳过 pip install
  python build.py --reinstall      # 强制重装依赖
  python build.py --jobs 4         # PyInstaller 并行线程数
"""

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── PySide6 子模块排除（gitgo 只用 QtCore/QtWidgets/QtGui）─────────────
_PYSIDE6_UNUSED = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel", "PySide6.QtNetwork",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtSql", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtWebSockets", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtTextToSpeech",
    "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtHelp", "PySide6.QtXml", "PySide6.QtXmlPatterns",
    "PySide6.QtPrintSupport", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQml", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtConcurrent", "PySide6.QtTest", "PySide6.QtStateMachine",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets",
]

# ── Hidden imports（保持完整）───────────────────────────────────────────
_HIDDEN_IMPORTS = [
    "PySide6.QtCore", "PySide6.QtWidgets", "PySide6.QtGui",
    "rich",
    "backend.core", "backend.core.config", "backend.core.sync_session",
    "backend.core.i18n", "backend.core.history", "backend.core.plugin",
    "backend.core.plugin_loader", "backend.core.migrate",
    "backend.core.operations", "backend.core.daemon",
    "backend.models",
    "backend.adapters.file_adapter", "backend.adapters.git_runner",
    "backend.adapters.local_file_adapter", "backend.adapters.local_git_runner",
    "backend.adapters.ssh_file_adapter", "backend.adapters.ssh_git_runner",
    "backend.adapters.factory",
    "backend.remote",
    "cli", "cli.commands",
    "frontend", "frontend.gui_main",
    "cui", "cui.main",
    "httpx",
    "paramiko",
]


def _hash_file(path: Path) -> str:
    """计算文件 SHA256 摘要"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _install_deps(reinstall: bool = False):
    """条件安装运行时依赖（哈希校验，避免重复安装）"""
    script_dir = Path(__file__).parent.resolve()
    req_path = script_dir / "requirements.txt"
    hash_path = script_dir / ".build_deps_hash"

    current_hash = _hash_file(req_path)

    if not reinstall and hash_path.exists():
        saved = hash_path.read_text().strip()
        if saved == current_hash:
            print("  [skip] dependencies unchanged (use --reinstall to force)")
            return

    print("=== Installing runtime dependencies ===")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(req_path)],
        check=True,
    )
    hash_path.write_text(current_hash)


def _pyinstaller_args(script_dir: Path, dist_dir: Path, work_dir: Path,
                      name: str, entry: Path, noconsole: bool = False,
                      fast: bool = False, jobs: int | None = None) -> list:
    """组装 PyInstaller 命令行参数"""
    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
        "--name", name,
        "--log-level", "WARN",
        "--noconfirm",
    ]

    if noconsole:
        args.append("--noconsole")

    if fast:
        args.append("--noupx")

    if jobs:
        args.extend(["--jobs", str(jobs)])

    # 已有的第三方排除
    for m in ["numpy", "PIL", "cv2", "matplotlib", "scipy", "pandas"]:
        args.extend(["--exclude-module", m])

    # PySide6 子模块排除
    for m in _PYSIDE6_UNUSED:
        args.extend(["--exclude-module", m])

    # Hidden imports
    for h in _HIDDEN_IMPORTS:
        args.extend(["--hidden-import", h])

    # Icon
    icon_path = script_dir / "gitgo_icon.png"
    if icon_path.exists():
        args.extend(["--icon", str(icon_path)])
        args.extend(["--add-data", f"{icon_path}{os.pathsep}."])

    # Locales
    locales_dir = script_dir / "locales"
    if locales_dir.exists():
        args.extend(["--add-data", f"{locales_dir}{os.pathsep}locales"])

    # Plugins
    plugins_dir = script_dir / "plugins"
    if plugins_dir.exists():
        args.extend(["--add-data", f"{plugins_dir}{os.pathsep}plugins"])

    args.append(str(entry))
    return args


def _build_exe(script_dir: Path, dist_dir: Path, work_dir: Path,
               name: str, entry: Path, noconsole: bool = False,
               fast: bool = False, jobs: int | None = None):
    """执行单个 PyInstaller 构建并返回产物路径"""
    args = _pyinstaller_args(
        script_dir, dist_dir, work_dir, name, entry,
        noconsole=noconsole, fast=fast, jobs=jobs,
    )
    print(f"\n=== Building {name}.exe ===")
    subprocess.run(args, check=True)
    exe_path = dist_dir / f"{name}.exe"
    if exe_path.exists():
        size_mb = exe_path.stat().st_size / 1024 / 1024
        print(f"  [OK] {exe_path} ({size_mb:.1f} MB)")
        return exe_path
    return None


def _clean_build_dirs(build_dir: Path, names: list[str]):
    """清理指定名称的 build 子目录"""
    for name in names:
        d = build_dir / name
        if d.exists():
            print(f"  Cleaning {d}...")
            try:
                shutil.rmtree(d)
            except PermissionError:
                print(f"  Warning: cannot delete {d}, skipping")


def _clean_old_exes(dist_dir: Path, names: list[str]):
    """清理旧 exe，失败则直接退出（否则 PyInstaller 覆盖写入也会失败）"""
    failed = []
    for name in names:
        p = dist_dir / f"{name}.exe"
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                failed.append(p)
    if failed:
        print("\n  [ERROR] 以下文件被占用，请关闭后重试：")
        for p in failed:
            print(f"    {p}")
        sys.exit(1)


def main():
    script_dir = Path(__file__).parent.resolve()
    os.chdir(script_dir)

    is_debug = "--debug" in sys.argv
    is_clean = "--clean" in sys.argv
    is_fast = "--fast" in sys.argv
    skip_install = "--skip-install" in sys.argv or is_fast
    reinstall = "--reinstall" in sys.argv
    jobs = None

    for i, arg in enumerate(sys.argv):
        if arg == "--jobs" and i + 1 < len(sys.argv):
            try:
                jobs = int(sys.argv[i + 1])
            except ValueError:
                pass

    dist_dir = script_dir / "dist"
    build_dir = script_dir / "build"
    dist_dir.mkdir(parents=True, exist_ok=True)

    mode_name = "DEBUG" if is_debug else "RELEASE"
    if is_fast:
        mode_name += " (fast)"

    print("=" * 50)
    print(f"  gitgo Build · {mode_name}")
    if is_clean:
        print("  (clean: 清缓存全量构建)")
    print("=" * 50)

    t0 = time.time()

    # ── Step 1: 依赖安装 ────────────────────────────────────────────
    if not skip_install:
        _install_deps(reinstall=reinstall)
    else:
        print("  [skip] pip install")

    # ── Step 2: 清理（仅 --clean 时）────────────────────────────────
    if is_clean:
        if is_debug:
            _clean_build_dirs(build_dir, ["gitgo_core", "gitgo_debug"])
        else:
            _clean_build_dirs(build_dir, ["gitgo"])
    # 始终清理目标 exe（PyInstaller 覆盖写入有时不刷新时间戳）
    if is_debug:
        _clean_old_exes(dist_dir, ["gitgo_debug", "gitgo_core"])
    else:
        _clean_old_exes(dist_dir, ["gitgo"])

    # ── Step 3: 构建 ────────────────────────────────────────────────
    if is_debug:
        core = _build_exe(
            script_dir, dist_dir, build_dir / "gitgo_core",
            "gitgo_core", script_dir / "__main__.py",
            noconsole=False, fast=is_fast, jobs=jobs,
        )
        launcher = _build_exe(
            script_dir, dist_dir, build_dir / "gitgo_debug",
            "gitgo_debug", script_dir / "debug_launcher.py",
            noconsole=False, fast=is_fast, jobs=jobs,
        )

        # Bat file
        exe_path = dist_dir / "gitgo_debug.exe"
        if exe_path.exists():
            bat_path = dist_dir / "run_debug.bat"
            bat_path.write_text(
                "@echo off\r\n"
                "title gitgo Debug - CLOSE ONLY WITH X\r\n"
                "echo ============================================\r\n"
                "echo   gitgo Debug (Parent: cmd.exe)\r\n"
                "echo   Console survives C++ segfault\r\n"
                "echo   NO keyboard shortcut can close this\r\n"
                "echo ============================================\r\n"
                "echo.\r\n"
                f'"{exe_path}" %*\r\n'
                "echo.\r\n"
                "echo ============================================\r\n"
                "echo   App exited (code: %%ERRORLEVEL%%)\r\n"
                ":wait_quit\r\n"
                "set /p _done=Type quit + Enter to close: \r\n"
                "if /i not \"%%_done%%\"==\"quit\" goto wait_quit\r\n"
                "exit\r\n",
                encoding="ascii"
            )
            t1 = time.time()
            print(f"\n{'='*50}")
            print(f"  [OK] Debug build complete ({t1 - t0:.1f}s)")
            if core:
                print(f"  Core:     {core} ({core.stat().st_size/1024/1024:.1f} MB)")
            if launcher:
                print(f"  Launcher: {launcher} ({launcher.stat().st_size/1024/1024:.1f} MB)")
            print(f"  Bat:      {bat_path}")
            print(f"{'='*50}")
        else:
            print("\n=== Build FAILED ===")
            sys.exit(1)
    else:
        exe = _build_exe(
            script_dir, dist_dir, build_dir / "gitgo",
            "gitgo", script_dir / "__main__.py",
            noconsole=True, fast=is_fast, jobs=jobs,
        )
        t1 = time.time()
        if exe:
            print(f"\n{'='*50}")
            print(f"  [OK] Release build complete ({t1 - t0:.1f}s)")
            print(f"  {exe} ({exe.stat().st_size/1024/1024:.1f} MB)")
            print(f"{'='*50}")
        else:
            print("\n=== Build FAILED ===")
            sys.exit(1)


if __name__ == "__main__":
    main()
