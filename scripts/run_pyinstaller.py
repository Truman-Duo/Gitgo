"""Run PyInstaller from an explicit build-only package directory.

Gitgo's embedded runtime keeps its activated application packages immutable.
Release builders may keep PyInstaller in a separate package set so the shipped
runtime does not need to expose build tooling.  This launcher adds only that
directory (and its deterministic pywin32 subpaths) before invoking PyInstaller.
"""
from __future__ import annotations

from pathlib import Path
import runpy
import sys


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: run_pyinstaller.py PACKAGE_DIR [PYINSTALLER_ARGS...]")
    packages = Path(sys.argv.pop(1)).resolve()
    if not (packages / "PyInstaller").is_dir():
        raise SystemExit(f"PyInstaller package not found under {packages}")
    sys.path[:0] = [
        str(packages),
        str(packages / "win32"),
        str(packages / "win32" / "lib"),
    ]
    runpy.run_module("PyInstaller", run_name="__main__")


if __name__ == "__main__":
    main()
