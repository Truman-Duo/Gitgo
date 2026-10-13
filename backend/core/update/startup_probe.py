"""Bounded real Native Host initialization/correlation test in an empty profile.

No project, provider or network operation is dispatched. This is not terminal
rendering acceptance, nor an OS sandbox. Parent-owned containment is separate.
"""
import io
import json
import os
import stat
from pathlib import Path

from .runtime_contract import data_probe
from .validation import reject


def startup_probe(nonce):
    report = data_probe(nonce)
    root = Path(os.environ.get("TMPDIR", ""))
    if not root.is_absolute() or root != root.resolve(strict=True):
        reject("UPDATE_HEALTH_FAILED", "Startup probe requires a private isolated profile")
    info = root.lstat()
    if root.is_symlink() or not root.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        reject("UPDATE_HEALTH_FAILED", "Startup profile ownership is invalid")
    for ancestor in root.parents:
        info = ancestor.lstat()
        if (ancestor.is_symlink() or info.st_uid not in (0, os.getuid())
                or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
            reject("UPDATE_HEALTH_FAILED", "Startup profile parent ownership is invalid")
    expected = {"GITGO_CONFIG_PATH": "config.json", "GITGO_STATE_HOME": "state",
                "GITGO_LLM_CONFIG_PATH": "providers.json", "GITGO_LLM_SECRET_PATH": "secrets.json"}
    if any(os.environ.get(key) != str(root / name) for key, name in expected.items()) or any(root.iterdir()):
        reject("UPDATE_HEALTH_FAILED", "Startup probe profile must be empty and isolated")
    from backend.core.native_host import NativeHost, PROTOCOL_VERSION
    output = io.StringIO()
    host = NativeHost(stdin=io.StringIO(), stdout=output)
    try:
        for operation in ("host.capabilities", "product.version"):
            host._handle_request({"protocol_version": PROTOCOL_VERSION, "type": "request",
                "request_id": nonce + operation, "operation": operation, "arguments": {}})
        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        if len(messages) != 2 or any(message.get("protocol_version") != 1 or message.get("type") != "response"
                or message.get("ok") is not True for message in messages):
            reject("UPDATE_HEALTH_FAILED", "Native Host protocol startup failed")
        capabilities, version = messages
        if (capabilities.get("request_id") != nonce + "host.capabilities"
                or capabilities["result"].get("protocol_version") != 1
                or "update.request_apply" not in capabilities["result"].get("operations", [])
                or version.get("request_id") != nonce + "product.version"
                or version["result"] != {"product_id": report["product_id"], "version": report["version"]}):
            reject("UPDATE_HEALTH_FAILED", "Native Host capability/identity startup failed")
    finally:
        host.close()
    return {**report, "native_host_check": True}
