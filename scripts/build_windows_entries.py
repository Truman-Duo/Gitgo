"""Create the managed Setup and single-file Portable from one Windows archive."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
PORTABLE_FOOTER = b"GITGO-WINDOWS-PORTABLE-v1\0"


def build(archive, output, compiler):
    with ZipFile(archive) as bundle:
        inputs = set(bundle.namelist())
        if not {"gitgo.exe", "internal/gitgo-host/gitgo-host.exe", "install-managed.ps1",
                "assets/brand/gitgo.ico", "source-notices/Gitgo-LICENSE.md", "build-manifest.json", "release-trust.json"} <= inputs:
            raise ValueError("Expected a complete Windows managed archive")
        product = json.loads(bundle.read("product.json"))
        icon_bytes = bundle.read("assets/brand/gitgo.ico")
        license_bytes = bundle.read("source-notices/Gitgo-LICENSE.md")
    if (product["platform"], product["architecture"], product["installation_mode"]) != (
            "windows", "x86_64", "managed-windows-v1"):
        raise ValueError("Unsupported Windows managed payload")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="windows-entries-", dir=output) as scratch:
        scratch = Path(scratch)
        with archive.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        checksum = scratch / "archive.sha256"
        checksum.write_text(digest + "\n", encoding="ascii")
        icon = scratch / "gitgo.ico"
        icon.write_bytes(icon_bytes)
        license_file = scratch / "license.txt"
        license_file.write_bytes(license_bytes)
        setup_flags = ["/define:SETUP", "/r:System.Drawing.dll", "/r:System.Web.Extensions.dll",
                       f"/resource:{license_file},Gitgo.License", str(ROOT / "packaging/windows/SetupWizard.cs")]
        for stem, target, flags in (("GitgoPortable", "exe", []), ("GitgoSetup", "winexe", setup_flags)):
            name = f"{stem}-{product['version']}-win-x64.exe"
            result = scratch / name
            subprocess.run([str(compiler), "/nologo", f"/t:{target}", "/platform:x64", "/optimize+", f"/win32icon:{icon}",
                "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll", "/r:System.Windows.Forms.dll",
                f"/resource:{archive},Gitgo.Payload", f"/resource:{checksum},Gitgo.PayloadSha256",
                f"/out:{result}", *flags, str(ROOT / "packaging/windows/DistributionEntry.cs")], check=True)
            if stem == "GitgoPortable":
                with result.open("ab") as stream:
                    stream.write(PORTABLE_FOOTER)
            with result.open("rb") as stream:
                binary_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result.replace(output / name)
            (output / (name + ".sha256")).write_text(f"{binary_digest}  {name}\n", encoding="ascii")
            print(output / name, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    options = parser.parse_args()
    build(options.archive.resolve(strict=True), options.output.resolve(), compiler.resolve(strict=True))


if __name__ == "__main__":
    main()
