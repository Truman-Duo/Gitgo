"""Build two independent Windows versions inside disposable hosted CI only.

No push, PR, Release, permanent install, private signing key, model or user data.
Artifacts stay in the runner's private temporary profile, never published.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(command, cwd):
    subprocess.run([str(item) for item in command], cwd=cwd, check=True)


def main():
    if (sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise SystemExit("Only a disposable GitHub-hosted Windows runner is supported")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bun", type=Path, required=True)
    args = parser.parse_args()
    from backend.core.update.windows_storage import validate_staging
    base = Path(tempfile.mkdtemp(prefix="gitgo-two-windows-")).resolve(strict=True)
    validate_staging(base)
    checkout = base / "source"
    run(["git", "clone", "--no-hardlinks", "--no-local", ROOT, checkout], ROOT)
    run(["git", "checkout", subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()], checkout)
    run([args.bun, "install", "--frozen-lockfile"], checkout / "cli/dashboard")
    previous, candidate = base / "previous", base / "candidate"
    def build(stage):
        run(["pwsh.exe", "-NoProfile", "-File", checkout / "packaging/build_windows.ps1",
            "-Python", sys.executable, "-Bun", args.bun, "-Ripgrep", os.environ["GITGO_RIPGREP_PATH"],
            "-RipgrepNotices", os.environ["GITGO_RIPGREP_NOTICES"], "-Output", stage,
            "-ArtifactOutput", base / (stage.name + "-archives"), "-ManagedUpdate"], checkout)
        evidence = json.loads((stage / "build-manifest.json").read_bytes())
        if evidence["source"]["dirty"] is not False:
            raise RuntimeError("Dirty build cannot be signed even in the two-version test")
    build(previous)
    version = "1.0.0-alpha.3.windows.1"
    product_path = checkout / "packaging/product.json"
    product = json.loads(product_path.read_bytes())
    old_version = product["version"]
    product.update(version=version, windows_version="1.0.0.3")
    product_path.write_text(json.dumps(product, indent=2) + "\n", encoding="utf-8")
    frontend_path = checkout / "cli/dashboard/package.json"
    frontend = json.loads(frontend_path.read_bytes())
    if frontend["version"] != old_version: raise RuntimeError("Previous source version mismatch")
    frontend["version"] = version
    frontend_path.write_text(json.dumps(frontend, indent=2) + "\n", encoding="utf-8")
    backend_path = checkout / "backend/version.py"
    source = backend_path.read_text(encoding="utf-8")
    marker = '__version__ = "' + old_version + '"'
    if source.count(marker) != 1: raise RuntimeError("Unrecognized backend version declaration")
    backend_path.write_text(source.replace(marker, '__version__ = "' + version + '"'), encoding="utf-8")
    run(["git", "add", "packaging/product.json", "cli/dashboard/package.json", "backend/version.py"], checkout)
    run(["git", "-c", "user.name=Windows CI", "-c", "user.email=windows-ci@local.invalid",
         "commit", "-m", "test(runtime): independently build second Windows version"], checkout)
    build(candidate)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        output.write("GITGO_UPDATE_PREVIOUS_STAGE=" + str(previous) + "\n")
        output.write("GITGO_UPDATE_CANDIDATE_STAGE=" + str(candidate) + "\n")
    print("Two independently committed frozen Windows test bundles built; no package published")


if __name__ == "__main__": main()
