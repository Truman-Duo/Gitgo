"""Assemble a closed Windows storage runtime in an explicit protected empty root.

This build tool never downloads, installs, changes ACLs, creates a Host registry,
or approves its own output. A trusted installer must approve the resulting
manifest independently and keep its complete native pins through use.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import zipfile

from backend.core.storage.native_windows_scope import WindowsScopeLibrary
from backend.core.storage.native_windows_tree import WindowsTreeLibrary, TreePurpose
from backend.core.storage.runtime_installation import PinnedRuntime
from backend.core.storage.runtime_manifest import (
    MANIFEST_NAME, RuntimeContent, RuntimeManifest, RuntimeMember, digest,
)

DEPENDENCIES = ("httpx", "httpcore", "h11", "anyio", "idna", "certifi", "click", "rich", "pygments",
                "markdown_it", "mdurl", "typing_extensions.py")
NATIVE_LIBRARIES = tuple(f"gitgo-storage-{kind}.dll" for kind in ("scope", "process", "journal", "tree", "sqlite", "cas_writer", "cas_generation"))


def archive(entries):
    """Stable bytes; host timestamps and absolute source paths are not embedded."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        seen = set()
        for name, source in sorted(entries):
            if name.casefold() in seen:
                raise ValueError("Duplicate archive member")
            seen.add(name.casefold())
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            target.writestr(info, source.read_bytes())
    return buffer.getvalue()


def inputs(repository: Path, python_root: Path, packages: Path, native_root: Path, profile: str, *, profile_file: Path | None = None):
    if sys.platform != "win32" or sys.version_info[:2] != (3, 14) or struct.calcsize("P") != 8:
        raise RuntimeError("This build profile requires native Windows CPython 3.14 x64")
    if sys.getwindowsversion().major<10:
        raise RuntimeError("This native runtime profile requires Windows 10 or newer")
    digest(profile)
    if python_root.resolve(strict=True) != Path(sys.base_prefix).resolve(strict=True):
        raise ValueError("The input Python installation must be the actual build interpreter")
    descriptor = None
    if profile_file is not None:
        from backend.core.storage.project_profile import ProjectProfile
        with profile_file.resolve(strict=True).open("rb") as source:
            descriptor = source.read(8193)
        if not 0 < len(descriptor) <= 8192 or ProjectProfile.from_bytes(descriptor).sha256 != profile:
            raise ValueError("Explicit storage profile does not match the requested content contract")
    files = {}
    def add(name, data):
        if name.casefold() in {key.casefold() for key in files}:
            raise ValueError("Duplicate package member")
        files[name] = data
    if descriptor is not None:
        add("storage-profile.json", descriptor)
    add("python.exe", (python_root/"python.exe").read_bytes())
    for source in sorted(python_root.glob("*.dll")):
        # CPython 3.14 supports Windows 10+. Its UCRT is always supplied by
        # Windows, even when a Conda input includes downlevel local copies.
        # https://learn.microsoft.com/cpp/windows/universal-crt-deployment
        # Excluding those copies preserves the strict packaged-vs-system role
        # check; it does not allow a package DLL to resolve from elsewhere.
        if source.name.lower()=="ucrtbase.dll" or source.name.lower().startswith("api-ms-win-"):
            continue
        add(source.name, source.read_bytes())
    for source in sorted((python_root/"DLLs").iterdir()):
        if source.is_file() and source.suffix.lower() in {".pyd", ".dll"}:
            add("DLLs/"+source.name, source.read_bytes())
    # The qualified Conda input may place these extension dependencies here.
    # The regular CPython input already keeps them beside its extension modules.
    for pattern in ("sqlite3.dll", "*ffi*.dll", "libcrypto*.dll", "libssl*.dll", "zlib*.dll", "bz2*.dll", "liblzma*.dll"):
        for source in sorted((python_root/"Library"/"bin").glob(pattern)):
            add("DLLs/"+source.name, source.read_bytes())
    library = python_root/"Lib"
    standard = []
    for source in sorted(library.rglob("*.py")):
        relative = source.relative_to(library)
        if relative.parts[0] not in {"site-packages", "test", "idlelib", "tkinter", "ensurepip"}:
            standard.append((relative.as_posix(), source))
    add("python314.zip", archive(standard))
    application = [(source.relative_to(repository).as_posix(), source)
                   for source in (repository/"backend").rglob("*.py") if "__pycache__" not in source.parts]
    for name in DEPENDENCIES:
        base = packages/name
        if base.is_file():
            application.append((name, base))
        elif base.is_dir():
            for source in base.rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts and source.suffix in {".py", ".pem", ".typed"}:
                    application.append((source.relative_to(packages).as_posix(), source))
        else:
            raise ValueError("Required closed package dependency is absent: "+name)
    add("app.zip", archive(application))
    add("python314._pth", b"python314.zip\nDLLs\napp.zip\n.\n")
    for name in NATIVE_LIBRARIES:
        add(name, (native_root/name).read_bytes())
    content = RuntimeContent("cpython-314-win_amd64", profile,
        tuple(RuntimeMember(name, len(data), hashlib.sha256(data).hexdigest()) for name, data in sorted(files.items())))
    return files, content


def build(repository: Path, python_root: Path, packages: Path, native_root: Path, output: Path, profile: str, *, profile_file: Path | None = None):
    paths = [path.resolve(strict=True) for path in (repository, python_root, packages, native_root, output)]
    repository, python_root, packages, native_root, output = paths
    if any(output == source or output.is_relative_to(source) or source.is_relative_to(output)
           for source in (repository, python_root, packages, native_root)):
        raise ValueError("Runtime output must be separate from all build inputs")
    files, content = inputs(repository, python_root, packages, native_root, profile, profile_file=profile_file)
    # Explicit build inputs, not production module discovery. No environment
    # variable selects a production DLL or supplies installation approval.
    scopes = WindowsScopeLibrary(ctypes.CDLL(str(native_root/"gitgo-storage-scope.dll")))
    trees = WindowsTreeLibrary(ctypes.CDLL(str(native_root/"gitgo-storage-tree.dll")))
    scope = scopes.open_scope()
    proof = verification = None
    try:
        sid, _ = scope.account()
        root = scope.root(str(output))
        scope.volume(root)
        root_identity = scope.identity(root)[0]
        proof = trees.reserve(TreePurpose.PIN, sid)
        proof.root(root, root_identity)
        if proof.inventory(0):
            raise ValueError("Runtime output must be an existing protected empty directory")
        for directory in content.directories:
            (output/directory).mkdir()
        for name, data in sorted(files.items()):
            with (output/name).open("xb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
        manifest = RuntimeManifest(str(output), root_identity, content)
        with (output/MANIFEST_NAME).open("xb") as target:
            target.write(manifest.to_bytes())
            target.flush()
            os.fsync(target.fileno())
        # Build verification only. The caller must independently approve this
        # digest; the returned bytes are not a Host permission or capacity ticket.
        verification = PinnedRuntime(trees, manifest.to_bytes(), manifest.sha256, sid)
        verification.pin()
    finally:
        if verification is not None: verification.close()
        if proof is not None: proof.close()
        scope.close()
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("repository", "python-root", "packages", "native-root", "output"):
        parser.add_argument("--"+option, required=True, type=Path)
    parser.add_argument("--storage-profile", required=True)
    parser.add_argument("--profile-file", type=Path)
    args = parser.parse_args()
    manifest = build(args.repository, args.python_root, args.packages, args.native_root, args.output, args.storage_profile, profile_file=args.profile_file)
    print(json.dumps(dict(root=manifest.root, manifest=manifest.sha256, content=manifest.content.contract,
                          files=len(manifest.content.files), bytes=sum(item.length for item in manifest.content.files))))


if __name__ == "__main__":
    main()
