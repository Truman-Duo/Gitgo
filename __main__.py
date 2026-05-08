"""CLI 入口: python -m sync_tool [--mode gui|cui|config]
   也可作为 PyInstaller 打包入口（使用静态导入确保分析器追踪到所有模块）"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# 确保自身在 sys.path 上（兼容 python __main__.py 直接运行）
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))
_meipass = getattr(sys, "_MEIPASS", None)
if _meipass and _meipass not in sys.path:
    sys.path.insert(0, _meipass)

# 使用绝对导入（兼容 -m 和直接运行两种方式）
from config import Config, ConfigManager
from cui_main import entry as cui_entry, setup_wizard
from gui_main import entry as gui_entry


def main():
    parser = argparse.ArgumentParser(
        description="工作区 -> 备份仓库同步工具"
    )
    parser.add_argument(
        "--mode",
        choices=["gui", "cui", "config"],
        default="gui",
        help="启动模式: gui(默认) | cui(终端) | config(仅配置)",
    )
    args = parser.parse_args()

    try:
        if args.mode == "gui":
            # 在冻结模式写 crash log
            if getattr(sys, "frozen", False):
                import tempfile
                _log = Path(tempfile.gettempdir()) / "sync_tool_startup.log"
                _log.write_text(f"Starting GUI mode at {__import__('datetime').datetime.now()}\n", encoding="utf-8")
            gui_entry()
        elif args.mode == "cui":
            cui_entry()
        elif args.mode == "config":
            cfg = ConfigManager.load()
            if cfg.projects:
                print(f"共 {len(cfg.projects)} 个项目:\n")
                for i, p in enumerate(cfg.projects, 1):
                    ws = p.workspace_path or "(使用当前目录)"
                    base = p.sync_base[:12] if p.sync_base else "无"
                    prefix = p.commit_format.get("prefix", "")
                    print(f"  [{i}] {p.name}")
                    print(f"      工作区: {ws}")
                    print(f"      备份库: {p.backup_path}")
                    print(f"      Commit前缀: {prefix}  Sync基点: {base}")
                    print()
            else:
                print("未配置任何项目")
    except Exception as e:
        msg = f"启动失败:\n{traceback.format_exc()}"
        print(msg, file=sys.stderr)
        # 写入日志文件
        import tempfile
        (Path(tempfile.gettempdir()) / "sync_tool_crash.log").write_text(msg, encoding="utf-8")
        # 冻结模式弹错误框
        if getattr(sys, "frozen", False):
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication(sys.argv)
                QMessageBox.critical(None, "同步工具 - 错误", msg)
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
