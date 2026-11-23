"""Stage native entry resources into the existing release inventory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.native_entry import LOCKED_SOURCE, _json, _safe


def stage_entry(stage: Path, launcher: Path | None = None):
    stage = _safe(stage).resolve(strict=True)
    product = _json(stage / "product.json")
    brand = _json(stage / "assets/brand/manifest.json")
    if product["product_id"] != "gitgo-terminal" or brand["source"]["sha256"] != LOCKED_SOURCE:
        raise ValueError("Native entry product or icon source changed")
    target = stage / "native-entry.json"
    if target.exists():
        raise ValueError("Native entry contract already staged")
    if product["platform"] == "macos":
        if launcher is None:
            raise ValueError("macOS requires its compiled Mach-O launcher")
        data = _safe(launcher).read_bytes()
        if data[:4] not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
            raise ValueError("macOS launcher is not Mach-O")
        app = stage / "Gitgo.app/Contents"
        app.mkdir(parents=True)
        (app / "MacOS").mkdir()
        (app / "Resources").mkdir()
        shutil.copyfile(launcher, app / "MacOS/Gitgo")
        (app / "MacOS/Gitgo").chmod(0o755)
        shutil.copyfile(stage / "assets/brand/gitgo.icns", app / "Resources/gitgo.icns")
        info = {"CFBundleExecutable": "Gitgo", "CFBundleIdentifier": "org.gitgo.terminal",
                "CFBundleName": "Gitgo", "CFBundleDisplayName": "Gitgo",
                "CFBundleIconFile": "gitgo.icns", "CFBundlePackageType": "APPL",
                "CFBundleInfoDictionaryVersion": "6.0", "LSUIElement": True,
                "CFBundleShortVersionString": product["version"].split("-")[0],
                "CFBundleVersion": product["version"].split("-")[0]}
        (app / "Info.plist").write_bytes(plistlib.dumps(info))
        command = app / "Resources/launch.command"
        command.write_text('#!/bin/sh\nHERE="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd -P)"\nexec "$HERE/gitgo" "$@"\n')
        command.chmod(0o755)
    contract = {"schema": 1, "product_id": product["product_id"], "platform": product["platform"],
                "source_sha256": LOCKED_SOURCE, "entry_api": 1}
    target.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    return contract


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--launcher", type=Path)
    args = parser.parse_args()
    stage_entry(args.stage, args.launcher)
