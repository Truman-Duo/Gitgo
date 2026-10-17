"""Build the Windows storage ownership primitive in an explicit output root.

No downloads, installation, ACL edits, or runtime activation occur here. The
compiler is a build input; runtime DLL discovery never consults these options.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


def build(compiler: Path, output: Path, *, sqlite_header: Path, selftest: bool = False) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("The native ownership primitive currently targets Windows")
    sqlite_header = sqlite_header.resolve(strict=True)
    if sqlite_header.name != "sqlite3.h" or not sqlite_header.is_file():
        raise ValueError("Select the qualified SQLite sqlite3.h explicitly")
    compiler = compiler.resolve(strict=True)
    if compiler.name.lower() not in {"clang.exe", "zig.exe"}:
        raise ValueError("Select clang.exe or zig.exe explicitly")
    output = output.absolute()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Select a new empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    prefix = [str(compiler), *(["cc"] if compiler.name.lower() == "zig.exe" else [])]
    flags = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I" + str(sqlite_header.parent)]
    result = {}
    for name in ("scope", "process", "journal", "tree", "sqlite"):
        library = output / f"gitgo-storage-{name}.dll"
        subprocess.run([*prefix, *flags, "-shared", str(root / f"backend/native/storage_{name}_windows.c"),
                        "-ladvapi32", "-luserenv", "-o", str(library)], check=True, timeout=120)
        result[name + "_library"] = str(library)
        cases = ("tree_pins", "tree_install", "tree_bootstrap", "tree_retirement", "tree_inventory") if name == "tree" else (("scope", "volume") if name == "scope" else (name,))
        for case in cases if selftest else ():
            fault_name = case.replace("_", "-")
            executable = output / f"storage-{fault_name}-faults.exe"
            # Process and SQLite fixtures use main; the others use wmain.
            unicode_flags = ["-municode"] if case not in {"process", "sqlite"} and compiler.name.lower() == "zig.exe" else []
            subprocess.run([*prefix, *flags, *unicode_flags, str(root / f"tests/native/storage_{case}_faults.c"),
                            "-ladvapi32", "-luserenv", "-o", str(executable)], check=True, timeout=120)
            result[case + "_fault_tests"] = str(executable)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compiler", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--sqlite-header", type=Path, required=True)
    args = parser.parse_args()
    compiler = args.compiler
    if compiler is None:
        found = shutil.which("clang.exe") or shutil.which("zig.exe")
        if not found:
            raise RuntimeError("Pass --compiler with a supported C compiler")
        compiler = Path(found)
    print(json.dumps(build(compiler, args.output, sqlite_header=args.sqlite_header, selftest=args.selftest)))


if __name__ == "__main__":
    main()
