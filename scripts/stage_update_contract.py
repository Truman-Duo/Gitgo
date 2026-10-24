"""Opt-in update data evidence from both build and actual frozen Host runtime.

No signing keys, installation changes, user config, or real databases. Call
before build-manifest inventory. Missing/mismatched runtime evidence is fatal.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core.update.health import probe_host
from backend.core.update.runtime_contract import runtime_evidence
from backend.core.update.tree import encode
from backend.core.update.validation import json_object


def stage_contract(stage: Path) -> dict:
    stage = stage.resolve(strict=True)
    for name in ("update-evidence", "update-data-contract.json", "update-probe-result.json"):
        path = stage / name
        if path.exists() or path.is_symlink():
            raise ValueError("Update evidence output already exists; use a fresh staging directory")
    product = json_object((stage / "product.json").read_bytes(), 128 * 1024)
    if product.get("schema_version") != 2 or "layouts" in product:
        raise ValueError("Update evidence requires a rendered product manifest")
    host_name = "gitgo-host.exe" if product["platform"] == "windows" else "gitgo-host"
    host = stage / "internal/gitgo-host" / host_name
    compatibility, evidence = runtime_evidence()
    with tempfile.TemporaryDirectory(prefix="gitgo-data-probe-") as raw:
        result = probe_host(host, Path(raw).resolve())
        report = result["runtime"]
    for key in ("product_id", "version", "platform", "architecture"):
        if report[key] != product[key]:
            raise ValueError("Frozen data probe identity differs from rendered product")
    if report["data_compatibility"] != compatibility:
        raise ValueError("Frozen runtime writers differ from build evidence")
    # Never adopt/overwrite an existing evidence directory. A failed build may
    # leave diagnostics, but it cannot publish a partial authoritative contract.
    (stage / "update-evidence").mkdir(mode=0o700)
    for name, data in evidence.items():
        with (stage / name).open("xb") as output:
            output.write(data)
    contract = {"schema_version": 1, "data_compatibility": compatibility}
    for name, short in (("state_migrations", "state"), ("observability_migrations", "observability")):
        contract[name] = [{**item, "path": f"update-evidence/{short}-{item['version']}.sql"}
                          for item in compatibility[name]]
    for name in ("config", "provider"):
        path = "update-evidence/" + name + ".json"
        contract[name] = {"schema_version": compatibility[name + "_schema_version"], "path": path,
                          "sha256": hashlib.sha256(evidence[path]).hexdigest()}
    # Runtime-owned limitation, not a health-success claim supplied by a child.
    with (stage / "update-probe-result.json").open("xb") as output:
        output.write(encode({"schema_version": 1, "data_contract_verified": True,
                             "scope_cleanup": result["cleanup"], "update_commit_authorized": False}))
    # Publish the contract only after all evidence/diagnostics have succeeded.
    with (stage / "update-data-contract.json").open("xb") as output:
        output.write(encode(contract))
    return contract


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    args = parser.parse_args()
    stage_contract(args.stage)
    print("Frozen runtime data evidence staged; activation still requires managed trust and verified handoff")
