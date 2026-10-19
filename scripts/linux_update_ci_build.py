"""Build two real Linux update bundles in a disposable hosted CI checkout.

Only test reports leave the runner. No release, install or signing key is published.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(command, cwd):
    subprocess.run([str(item) for item in command], cwd=cwd, check=True)


def main():
    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise SystemExit("Only a disposable GitHub-hosted Linux runner is supported")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bun", type=Path, required=True)
    args = parser.parse_args()
    base = Path(tempfile.mkdtemp(prefix="gitgo-two-linux-")).resolve(strict=True)
    checkout = base / "source"
    run(["git", "clone", "--no-hardlinks", "--no-local", ROOT, checkout], ROOT)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    run(["git", "checkout", head], checkout)
    run([args.bun, "install", "--frozen-lockfile"], checkout / "cli/dashboard")
    previous, candidate = base / "previous", base / "candidate"

    def build(stage):
        run(["bash", checkout / "packaging/build_linux.sh", "--python", sys.executable,
             "--bun", args.bun, "--ripgrep", os.environ["GITGO_RIPGREP_PATH"],
             "--ripgrep-notices", os.environ["GITGO_RIPGREP_NOTICES"],
             "--output", stage, "--archive", base / (stage.name + ".tar.gz"),
             "--update-evidence"], checkout)
        evidence = json.loads((stage / "build-manifest.json").read_bytes())
        if evidence["source"]["dirty"] is not False:
            raise RuntimeError("Two-version acceptance requires a clean source commit")

    build(previous)
    version = "1.0.0-alpha.3.linux.1"
    product_path = checkout / "packaging/product.json"
    product = json.loads(product_path.read_bytes())
    old_version = product["version"]
    product.update(version=version, windows_version="1.0.0.3")
    product_path.write_text(json.dumps(product, indent=2) + "\n", encoding="utf-8")
    frontend_path = checkout / "cli/dashboard/package.json"
    frontend = json.loads(frontend_path.read_bytes())
    if frontend["version"] != old_version:
        raise RuntimeError("Previous source version mismatch")
    frontend["version"] = version
    frontend_path.write_text(json.dumps(frontend, indent=2) + "\n", encoding="utf-8")
    backend_path = checkout / "backend/version.py"
    source = backend_path.read_text(encoding="utf-8")
    marker = '__version__ = "' + old_version + '"'
    if source.count(marker) != 1:
        raise RuntimeError("Unrecognized backend version declaration")
    backend_path.write_text(source.replace(marker, '__version__ = "' + version + '"'), encoding="utf-8")
    run(["git", "add", "packaging/product.json", "cli/dashboard/package.json", "backend/version.py"], checkout)
    run(["git", "-c", "user.name=Linux CI", "-c", "user.email=linux-ci@local.invalid",
         "commit", "-m", "test(runtime): independently build second Linux version"], checkout)
    build(candidate)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        output.write("GITGO_UPDATE_PREVIOUS_STAGE=" + str(previous) + "\n")
        output.write("GITGO_UPDATE_CANDIDATE_STAGE=" + str(candidate) + "\n")
    print("Two independently committed Linux test bundles built; no package published")


if __name__ == "__main__":
    main()
