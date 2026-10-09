"""Executable identity checks before discovery probes or privileged execution.

Names and version banners are not identity evidence. Windows Git Bash needs
valid Windows trust signatures from the Git publisher on both wrappers and
the actual Bash engine. Unsigned installations are reported, never silently
treated as verified. Fingerprints bind the checked files to a later launch.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


def windows_system_executable(relative):
    # SystemRoot/PATH can be poisoned; obtain the system location from Windows.
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise ValueError("Windows system directory unavailable")
    return Path(buffer.value) / relative


def safe_probe_environment():
    sensitive = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY", "CREDENTIAL", "COOKIE", "AUTHORIZATION", "PRIVATE_KEY", "ACCESS_KEY", "BEARER")
    return {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in sensitive)
            and k.upper() not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "LD_PRELOAD", "LD_LIBRARY_PATH"}
            and not k.upper().startswith("BASH_FUNC_")}


def file_identity(path):
    path = Path(path).resolve(strict=True)
    before = path.stat()
    if not 0 < before.st_size <= 128 * 1024 * 1024:
        raise ValueError("Executable size exceeds the identity-check limit")
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise ValueError("Not a Windows executable image")
        handle.seek(0)
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    after = path.stat()
    # Reading may update access time. Only content/file identity changes matter.
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
        raise ValueError("Executable changed while identity was checked")
    return {"path": str(path), "sha256": digest}


def assert_identity_unchanged(identity):
    for file in identity["files"]:
        if file_identity(file["path"]) != {"path": file["path"], "sha256": file["sha256"]}:
            raise ValueError("Verified terminal image changed")
    for directory in identity.get("directories", []):
        names = sorted(p.name for p in Path(directory["path"]).iterdir() if p.suffix.lower() == ".dll")
        if names != directory["files"]:
            raise ValueError("Verified terminal dependency directory changed")


def windows_signatures(paths):
    powershell = windows_system_executable("System32/WindowsPowerShell/v1.0/powershell.exe")
    if not powershell.is_file():
        raise ValueError("Windows signature verifier unavailable")
    # Paths are JSON input, never interpolated into shell source.
    script = """$ErrorActionPreference='Stop';
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);
Import-Module ($PSHOME + '/Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1');
$paths = ($input | Out-String) | ConvertFrom-Json;
@($paths | ForEach-Object {
  $sig=Get-AuthenticodeSignature -LiteralPath $_;
  $version=[System.Diagnostics.FileVersionInfo]::GetVersionInfo($_);
  [PSCustomObject]@{path=$_;status=$sig.Status.ToString();subject=$sig.SignerCertificate.Subject;thumbprint=$sig.SignerCertificate.Thumbprint;original_filename=$version.OriginalFilename}
}) | ConvertTo-Json -Compress
"""
    result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                            input=json.dumps([str(p) for p in paths]), capture_output=True, encoding="utf-8",
                            errors="replace", timeout=10, creationflags=0x08000000, env=safe_probe_environment())
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError("Windows signature check failed")
    rows = json.loads(result.stdout)
    return [rows] if isinstance(rows, dict) else rows


def verify_windows_terminal(path, terminal_id, *, signature_reader=windows_signatures):
    """Explicit publisher policies; unsupported identities fail visibly closed.

    New publishers belong in this policy, not ad-hoc filename/version probes.
    App execution aliases must be resolved to their installed package image.
    """
    try:
        if terminal_id != "windows_terminal":
            raise ValueError("No publisher verification policy for this terminal; continuing in the current terminal")
        identity = file_identity(path)
        row = signature_reader([Path(identity["path"])])[0]
        if row.get("status") != "Valid" or not re.search(r"(?:^|,\s*)CN=Microsoft Corporation(?:,|$)", str(row.get("subject") or "")):
            raise ValueError("Terminal signature is not valid Microsoft publisher evidence")
        if str(row.get("original_filename") or "").lower() not in {"windowsterminal.exe", "wt.exe"}:
            raise ValueError("Signed file is not a Windows Terminal image")
        if file_identity(path) != identity:
            raise ValueError("Terminal changed during validation")
        identity.update({k: row[k] for k in ("status", "subject", "thumbprint")})
        return {"verified": True, "code": "VERIFIED_WINDOWS_TERMINAL", "files": [identity]}
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired) as exc:
        return {"verified": False, "code": "TERMINAL_IDENTITY_UNVERIFIED", "message": str(exc)[:500], "files": []}


def verify_git_bash(root: Path, *, signature_reader=windows_signatures, probe_runner=subprocess.run, package_verifier=None):
    root = root.resolve(strict=False)
    files = [root / "git-bash.exe", root / "bin/bash.exe", root / "usr/bin/bash.exe"]
    try:
        identities = [file_identity(path) for path in files]
        for identity in identities:
            Path(identity["path"]).relative_to(root)
        signatures = signature_reader(files)
        signatures = {str(Path(row["path"]).resolve()): row for row in signatures}
        for identity in identities:
            row = signatures.get(identity["path"], {})
            if row.get("status") != "Valid" or not re.search(r"(?:^|,\s*)CN=Johannes Schindelin(?:,|$)", str(row.get("subject") or "")):
                raise ValueError("Bash/launcher signature invalid, unsigned or not from the Git for Windows publisher")
            identity.update({k: row[k] for k in ("status", "subject", "thumbprint")})
        if len({i["thumbprint"] for i in identities}) != 1:
            raise ValueError("Git launcher and Bash engine publishers do not match")
        from backend.core.package_provenance import verify_git_terminal_package
        package = (package_verifier or verify_git_terminal_package)(root)
        if not package["verified"]:
            return package
        nonce = uuid.uuid4().hex
        probe = f'[[ -n "$BASH_VERSION" ]] && (( 19 + 23 == 42 )) && printf "gitgo:{nonce}:%s\\n" "$BASH_VERSION"'
        result = probe_runner([str(files[1]), "--noprofile", "--norc", "-c", probe],
                              cwd=root, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=4, creationflags=0x08000000, env=safe_probe_environment())
        if result.returncode or not re.fullmatch(rf"gitgo:{nonce}:\d+\.\d+[^\r\n]*\r?\n", result.stdout):
            raise ValueError("Signed Bash failed its isolated behavior check")
        if [file_identity(p) for p in files] != [{"path": i["path"], "sha256": i["sha256"]} for i in identities]:
            raise ValueError("Git Bash files changed during validation")
        assert_identity_unchanged(package)
        extra = [i for i in package["files"] if i["path"] not in {i["path"] for i in identities}]
        return {**package, "verified": True, "code": "VERIFIED_GIT_BASH", "files": [*identities, *extra],
                "bash_version": result.stdout.strip().rsplit(":", 1)[1]}
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        return {"verified": False, "code": "TERMINAL_IDENTITY_UNVERIFIED", "message": str(exc)[:500], "files": []}
