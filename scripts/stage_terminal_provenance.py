"""Stage inert image hashes from an official Git release, never execute/extract it.

The reference belongs to the application distribution, not user launcher config.
Runtime verification uses this offline reference; discovering a new installation
does not silently download code or trust a candidate-supplied manifest.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import time
import urllib.request
import urllib.error
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {"git-bash.exe", "bin/bash.exe", "usr/bin/bash.exe", "usr/bin/mintty.exe",
          "usr/bin/winpty.exe", "usr/bin/winpty-agent.exe", "usr/bin/winpty.dll", "usr/bin/msys-2.0.dll"}


def request(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Gitgo-terminal-provenance"}), timeout=30)


def stage(tag, arch):
    if not re.fullmatch(r"v\d+\.\d+\.\d+\.windows\.\d+", tag):
        raise ValueError("Invalid release tag")
    release_url = f"https://api.github.com/repos/git-for-windows/git/releases/tags/{tag}"
    version, revision = tag[1:].split(".windows.")
    filename = f"Git-{version}{'.' + revision if revision != '1' else ''}-{'64-bit' if arch == 'x64' else 'arm64'}.tar.bz2"
    expected_url = f"https://github.com/git-for-windows/git/releases/download/{tag}/{filename}"
    try:
        with request(release_url) as response:
            release = json.loads(response.read(2_000_001))
        asset = next(a for a in release["assets"] if a["name"] == filename)
    except urllib.error.HTTPError as error:
        if error.code not in {403, 429}:
            raise
        print("GitHub API rate-limited; reading the checksum from the official release page", flush=True)
        class Text(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
            def handle_data(self, value): self.parts.append(value)
        with request(f"https://github.com/git-for-windows/git/releases/tag/{tag}") as response:
            body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError("Official release page exceeds its bound")
        text = Text()
        text.feed(body.decode("utf-8"))
        match = re.search(re.escape(filename) + r"\s+([a-f0-9]{64})\b", " ".join(text.parts))
        if not match:
            raise ValueError("Official release page has no checksum for the exact asset")
        asset = {"digest": "sha256:" + match[1], "browser_download_url": expected_url, "size": 256 * 1024 * 1024 - 1}
    digest = str(asset.get("digest") or "").removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        match = re.search(r"(?m)^\|?\s*" + re.escape(filename) + r"\s*\|\s*`?([a-f0-9]{64})", release.get("body", ""))
        if not match:
            raise ValueError("Official asset has no published SHA256")
        digest = match[1]
    if asset["browser_download_url"] != expected_url or not 0 < asset["size"] < 256 * 1024 * 1024:
        raise ValueError("Unexpected official asset URL or size")
    cache = ROOT / ".gitgo/terminal-reference"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / filename
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        started = time.monotonic()
        total = 0
        with request(expected_url) as response, archive.with_suffix(".partial").open("wb") as output:
            expected_size = int(response.headers.get("Content-Length", "0"))
            if not 0 < expected_size <= asset["size"]:
                raise ValueError("Official archive size unavailable or exceeds its bound")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > asset["size"] or time.monotonic() - started > 600:
                    raise ValueError("Release download exceeded its bound")
                output.write(chunk)
                if total % (16 * 1024 * 1024) < 1024 * 1024:
                    print(f"Downloaded {total // (1024 * 1024)} MiB", flush=True)
        partial = archive.with_suffix(".partial")
        if total != expected_size or hashlib.sha256(partial.read_bytes()).hexdigest() != digest:
            raise ValueError("Official archive checksum mismatch")
        partial.replace(archive)
    files = {}
    started = time.monotonic()
    expanded = 0
    with tarfile.open(archive, mode="r|bz2") as package:
        for member in package:
            path = str(PurePosixPath(member.name.removeprefix("./")))
            expanded += max(0, member.size)
            if time.monotonic() - started > 180 or expanded > 2_000_000_000:
                raise ValueError("Archive scan exceeded its bound")
            if path not in IMAGES and not (path.startswith("usr/bin/") and path.lower().endswith(".dll")):
                continue
            if (PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
                    or "\\" in path or ":" in path):
                raise ValueError("Unsafe terminal reference path in archive")
            if not member.isfile() or member.size > 128 * 1024 * 1024 or path in files:
                raise ValueError("Reference image is not a unique bounded regular file")
            with package.extractfile(member) as image:
                files[path] = hashlib.file_digest(image, "sha256").hexdigest()
    if not IMAGES.issubset(files):
        raise ValueError(f"Release is missing required terminal images: {sorted(IMAGES - files.keys())}")
    manifest = {"schema_version": 1, "package": "git-for-windows", "release": tag, "architecture": arch,
                "source": f"https://github.com/git-for-windows/git/releases/tag/{tag}",
                "archive": {"url": expected_url, "sha256": digest}, "files": files}
    output = ROOT / "backend/resources/terminal_provenance" / f"git-for-windows-{tag}-{arch}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(output), "reference_images": len(files), "archive_sha256": digest}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--arch", choices=["x64", "arm64"], default="x64")
    args = parser.parse_args()
    stage(args.tag, args.arch)
