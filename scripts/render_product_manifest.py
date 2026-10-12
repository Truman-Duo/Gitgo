"""Render the shared product contract into one platform release manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex


def render_manifest(source: Path, platform: str, architecture: str = "") -> dict:
    manifest = json.loads(source.read_text(encoding="utf-8"))
    layouts = manifest.pop("layouts", {})
    if platform not in layouts:
        raise ValueError(f"product manifest has no layout for {platform!r}")
    command = str(manifest.get("primary_command") or "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", command) is None:
        raise ValueError(f"unsafe primary command: {command!r}")
    manifest["platform"] = platform
    if architecture:
        manifest["architecture"] = architecture
    manifest["layout"] = layouts[platform]
    return manifest


def render_shell_contract(manifest: dict) -> str:
    """Render installer fields as a shell-safe, sourceable contract."""
    aliases = manifest.get("command_aliases") or []
    values = {
        "GITGO_PRODUCT_ID": str(manifest.get("product_id") or ""),
        "GITGO_PRODUCT_PLATFORM": str(manifest.get("platform") or ""),
        "GITGO_PRODUCT_ARCHITECTURE": str(manifest.get("architecture") or ""),
        "GITGO_PRODUCT_COMMAND": str(manifest.get("primary_command") or ""),
        "GITGO_PRODUCT_ALIASES": " ".join(str(value) for value in aliases),
    }
    for value in values.values():
        if "\n" in value or "\r" in value:
            raise ValueError("shell contract values must be single-line")
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--architecture", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shell-output", type=Path)
    args = parser.parse_args()

    rendered = render_manifest(args.source, args.platform, args.architecture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(rendered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.shell_output is not None:
        args.shell_output.parent.mkdir(parents=True, exist_ok=True)
        args.shell_output.write_text(render_shell_contract(rendered), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
