"""Developer-side native checks for exact-source Unix preview release assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

TARGETS = {("macos", "arm64"), ("linux", "x86_64"), ("linux", "aarch64")}

def policy(source, target, architecture):
    if (target, architecture) not in TARGETS:
        raise ValueError("Unsupported Unix release target")
    value = json.loads((source / "packaging/release-trust/windows-x86_64.json").read_bytes())
    if set(value["channels"]) != {"preview"}:
        raise ValueError("Expected reviewed preview-only publisher policy")
    value["platform"], value["architecture"] = target, architecture
    for field in ("manifest_url", "signature_url"):
        old = value["channels"]["preview"][field]
        if "/updates-preview/windows-x86_64.manifest." not in old:
            raise ValueError("Unexpected source publisher feed")
        value["channels"]["preview"][field] = old.replace("windows-x86_64.manifest.", target+"-"+architecture+".manifest.")
    return value

def native_check(source, release, commit, published):
    from scripts.unix_release_manifest import verify
    from scripts.smoke_packaged_runtime import ProtocolClient
    from backend.core.config import Config, ConfigManager
    from backend.core.update.release_policy import parse_release_policy
    build = verify(release, commit)
    expected = policy(source, build["platform"], build["architecture"])
    if json.loads((release / "release-trust.json").read_bytes()) != expected:
        raise ValueError("Archive trust differs from reviewed publisher")
    parse_release_policy((release / "release-trust.json").read_bytes(), build["platform"], build["architecture"])
    with tempfile.TemporaryDirectory(prefix="gitgo-publisher-managed-") as raw:
        root = Path(raw).resolve()
        home = root / "home"
        home.mkdir(mode=0o700)
        env = {k:v for k,v in os.environ.items() if not k.startswith(("GITGO_", "PYTHON", "LD_", "DYLD_"))}
        env.update(HOME=str(home), PATH="/usr/bin:/bin:/usr/sbin:/sbin",
            XDG_DATA_HOME=str(home/".local/share"), XDG_STATE_HOME=str(home/".local/state"),
            GITGO_CONFIG_PATH=str(home/"config.json"), GITGO_STATE_HOME=str(home/"state"),
            GITGO_LLM_CONFIG_PATH=str(home/"llm.json"), GITGO_LLM_SECRET_PATH=str(home/"secrets.json"))
        ConfigManager.save(Config(), Path(env["GITGO_CONFIG_PATH"]), allow_empty=True)
        install, commands = root/"app", root/"bin"
        result = subprocess.run([str(release/"install.sh"), "--managed", "--no-path-update",
            "--install-root", str(install), "--bin-dir", str(commands)], env=env, check=True,
            capture_output=True, text=True, timeout=180)
        if json.loads(result.stdout).get("managed") is not True:
            raise ValueError("Installer did not register managed installation")
        client = ProtocolClient(install/"active/internal/gitgo-host/gitgo-host", env)
        try:
            def call(channel):
                value=client.call("update.status", {"channel":channel}, timeout=60)
                if not value.get("ok"):
                    raise RuntimeError("update.status: "+str(value.get("error")))
                return value["result"]
            preview, stable = call("preview"), call("stable")
            if preview.get("schema_version") != 6 or not preview.get("can_check"):
                raise ValueError("Installed preview trust unavailable: "+str(preview))
            if stable.get("can_check") or stable.get("status") != "unconfigured":
                raise ValueError("Stable and preview feeds mixed")
            strategy = "after_reboot" if build["platform"]=="macos" else "immediate"
            if preview.get("apply_strategy") != strategy:
                raise ValueError("Wrong update handoff strategy")
            if published:
                until=time.monotonic()+120
                while preview.get("automatic",{}).get("state")!="up_to_date":
                    if preview.get("automatic",{}).get("state")=="error" or time.monotonic()>until:
                        raise ValueError("Automatic public check failed: "+str(preview))
                    time.sleep(0.5)
                    preview=call("preview")
                if preview.get("latest_version")!=build["version"] or preview.get("can_download"):
                    raise ValueError("Automatic check selected another release")
        finally:
            client.close()
        return {"source":build["source"], "platform":build["platform"], "architecture":build["architecture"],
            "managed_install":True, "preview_trust":True, "stable_unconfigured":True,
            "apply_strategy":strategy, "public_automatic_check":published,
            "manual_check_invoked":False, "formal_terminal_verified":False}

def main():
    p=argparse.ArgumentParser()
    p.add_argument("mode", choices=("policy","managed","published"))
    p.add_argument("--source",type=Path,required=True)
    p.add_argument("--platform",choices=("macos","linux"))
    p.add_argument("--architecture",choices=("arm64","x86_64","aarch64"))
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--archive",type=Path)
    p.add_argument("--commit")
    a=p.parse_args()
    source=a.source.resolve(strict=True)
    sys.path.insert(0,str(source))
    if a.mode=="policy":
        from backend.core.update.release_policy import parse_release_policy
        raw=json.dumps(policy(source,a.platform,a.architecture),indent=2).encode()
        parse_release_policy(raw,a.platform,a.architecture)
        with a.output.open("xb") as f: f.write(raw)
        return
    if a.commit!=subprocess.check_output(["git","rev-parse","HEAD"],cwd=source,text=True).strip():
        raise ValueError("Checkout differs from requested immutable commit")
    from backend.core.update.archive import _extract_archive, ExtractionLimits
    from scripts.unix_release_manifest import verify
    with tempfile.TemporaryDirectory(prefix="gitgo-public-package-") as raw:
        work=Path(raw)
        if a.mode=="published":
            from backend.core.update.release_policy import parse_release_policy
            from backend.core.update.transport import HttpsTransport
            from backend.core.update.manifest import MAX_MANIFEST_BYTES, MAX_SIGNATURE_BYTES
            trust=policy(source,a.platform,a.architecture)
            parsed=parse_release_policy(json.dumps(trust).encode(),a.platform,a.architecture)
            channel=parsed.channel("preview")
            transport=HttpsTransport(channel.allowed_hosts,deadline=600)
            wire=transport.fetch_bytes(channel.manifest_url,MAX_MANIFEST_BYTES)
            signature=transport.fetch_bytes(channel.signature_url,MAX_SIGNATURE_BYTES)
            manifest=parsed.verifier.verify(wire,signature)
            if (manifest.source_commit!=a.commit or manifest.release_id!="v1.0.0-alpha.3"
                or manifest.scope!=("gitgo-terminal","preview",a.platform,a.architecture)):
                raise ValueError("Published identity mismatch")
            archive=work/"public.tar.gz"
            with archive.open("xb") as f:
                transport.receive(manifest.url,manifest.bytes,f,expected_size=manifest.bytes)
            with archive.open("rb") as f: actual=hashlib.file_digest(f,"sha256").hexdigest()
            if actual!=manifest.sha256:
                raise ValueError("Public archive digest mismatch")
            checksum=transport.fetch_bytes(manifest.url+".sha256",4096).decode("ascii")
            if checksum.split()[0]!=manifest.sha256:
                raise ValueError("Public checksum mismatch")
        else:
            archive=a.archive.resolve(strict=True)
            expected=Path(str(archive)+".sha256").read_text().split()[0]
            with archive.open("rb") as f:
                if hashlib.file_digest(f,"sha256").hexdigest()!=expected:
                    raise ValueError("Archive checksum mismatch")
        with archive.open("rb") as f:
            release=_extract_archive(f,"tar.gz",work/"payload",work,ExtractionLimits(),lambda:False)
        build=verify(release,a.commit)
        if a.mode=="published":
            from backend.core.update.contracts import verify_bundle_contract
            verify_bundle_contract(release,manifest)
            if build["source"]["tree"]!=manifest.source_tree:
                raise ValueError("Published tree mismatch")
        report=native_check(source,release,a.commit,a.mode=="published")
        with archive.open("rb") as f: report["archive_sha256"]=hashlib.file_digest(f,"sha256").hexdigest()
        report["publisher_signature_verified"]=a.mode=="published"
        a.output.write_text(json.dumps(report,indent=2)+"\n")
        print(json.dumps(report))

if __name__=="__main__": main()
