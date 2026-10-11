"""Offline package provenance for unsigned companions of signed applications.

References ship with the Host. Candidate directories and user config cannot
supply a reference. A signed anchor must match the same official package as
every executable/DLL used by this terminal before any behavior probe runs.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re

from backend.core.executable_identity import file_identity

REFERENCES = Path(__file__).resolve().parent.parent / "resources/terminal_provenance"
GIT_ANCHORS = ("git-bash.exe", "bin/bash.exe", "usr/bin/bash.exe")
GIT_TERMINAL_IMAGES = (*GIT_ANCHORS, "usr/bin/mintty.exe", "usr/bin/winpty.exe",
                       "usr/bin/winpty-agent.exe", "usr/bin/winpty.dll", "usr/bin/msys-2.0.dll")


def load_references(directory=REFERENCES):
    references = []
    paths = sorted(Path(directory).glob("git-for-windows-*.json"))
    if len(paths) > 32:
        raise ValueError("Too many bundled terminal package references")
    for path in paths:
        if path.stat().st_size > 256 * 1024:
            raise ValueError("Package reference exceeds its size limit")
        reference = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(reference, dict) or not isinstance(reference.get("archive"), dict):
            raise ValueError("Invalid bundled package reference")
        files = reference.get("files")
        release = reference.get("release", "")
        if (reference.get("schema_version") != 1 or reference.get("package") != "git-for-windows"
                or not re.fullmatch(r"v\d+\.\d+\.\d+\.windows\.\d+", release)
                or reference.get("architecture") not in {"x64", "arm64"}
                or reference.get("source") != f"https://github.com/git-for-windows/git/releases/tag/{release}"
                or not re.fullmatch(r"[a-f0-9]{64}", reference.get("archive", {}).get("sha256", ""))
                or not isinstance(files, dict) or not 0 < len(files) <= 256):
            raise ValueError("Invalid bundled package reference")
        for name, digest in files.items():
            parts = PurePosixPath(name)
            if (parts.is_absolute() or ".." in parts.parts or str(parts) != name or "\\" in name
                    or ":" in name or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise ValueError("Unsafe path or digest in package reference")
        if not set(GIT_TERMINAL_IMAGES).issubset(files):
            raise ValueError("Package reference lacks terminal images")
        references.append(reference)
    return references


def verify_git_terminal_package(root, *, reference_directory=REFERENCES):
    try:
        root = Path(root).resolve(strict=True)
        anchors = {name: file_identity(root / name) for name in GIT_ANCHORS}
        reference = next((ref for ref in load_references(reference_directory)
                          if all(ref["files"][name] == identity["sha256"] for name, identity in anchors.items())), None)
        if reference is None:
            raise ValueError("No bundled official-package reference matches this Git installation; use Current terminal or update Gitgo's references")
        # A DLL injected next to a checked image also changes the launch trust
        # decision. Inspect all three application search directories, including
        # extra names, instead of checking only the unsigned mintty.exe image.
        directories = []
        # Require the complete referenced dependency set. Otherwise deleting a
        # local DLL could make the loader find an unverified copy on PATH.
        names = set(reference["files"])
        for relative in (".", "bin", "usr/bin"):
            directory = (root / relative).resolve(strict=True)
            directory.relative_to(root)
            dlls = sorted(p.name for p in directory.iterdir() if p.suffix.lower() == ".dll")
            if len(dlls) > 256:
                raise ValueError("Terminal dependency directory exceeds its limit")
            directories.append({"path": str(directory), "files": dlls})
            names.update(str(PurePosixPath(relative) / name) for name in dlls)
        identities = []
        size = 0
        for name in sorted(names):
            if name not in reference["files"]:
                raise ValueError(f"Unreferenced terminal dependency: {name}")
            image = (root / name).resolve(strict=True)
            image.relative_to(root)
            size += image.stat().st_size
            if size > 512 * 1024 * 1024:
                raise ValueError("Terminal dependency images exceed their total limit")
            identity = file_identity(image)
            if identity["sha256"] != reference["files"][name]:
                raise ValueError(f"Terminal image differs from the official package: {name}")
            identities.append(identity)
        return {"verified": True, "code": "VERIFIED_GIT_PACKAGE", "files": identities,
                "directories": directories, "provenance": {key: reference[key] for key in
                    ("package", "release", "architecture", "source", "archive")}}
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"verified": False, "code": "TERMINAL_PACKAGE_UNVERIFIED", "message": str(error)[:500], "files": []}
