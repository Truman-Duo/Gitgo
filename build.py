"""PyInstaller 打包脚本 - 构建 sync_tool.exe"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    # 确保在脚本所在目录运行
    script_dir = Path(__file__).parent.resolve()
    os.chdir(script_dir)

    # 清理旧的打包产物
    dist_dir = script_dir / "dist"
    build_dir = script_dir / "build"
    spec_file = script_dir / "sync_tool.spec"

    for d in [dist_dir, build_dir]:
        if d.exists():
            shutil.rmtree(d)
    if spec_file.exists():
        spec_file.unlink()

    # 确保依赖已安装
    print("=== 检查依赖 ===")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
        check=True,
    )

    # 执行 PyInstaller
    print("\n=== 打包中 ===")
    pyinstaller_args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",               # 单文件 exe
        "--noconsole",             # 无控制台窗口
        "--name", "sync_tool",
        "--distpath", str(dist_dir),
        "--workpath", str(build_dir),
        "--hidden-import", "PySide6.QtCore",
        "--hidden-import", "PySide6.QtWidgets",
        "--hidden-import", "PySide6.QtGui",
        "--hidden-import", "rich",
        "--hidden-import", "sync_tool.core",
        "--hidden-import", "sync_tool.config",
        "--hidden-import", "sync_tool.gui_main",
        "--hidden-import", "sync_tool.cui_main",
    ]

    # 入口
    entry = script_dir / "__main__.py"
    pyinstaller_args.append(str(entry))

    subprocess.run(pyinstaller_args, check=True)

    # 产物路径
    exe_path = dist_dir / "sync_tool.exe"
    if exe_path.exists():
        print(f"\n=== 打包成功! ===")
        print(f"产物: {exe_path}")
        print(f"大小: {exe_path.stat().st_size / 1024 / 1024:.1f} MB")
        print("双击运行即可启动 GUI")
    else:
        print("\n=== 打包失败 ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
