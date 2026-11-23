"""Gitgo-only four-target update inputs, fixed before execution."""
import base64
import hashlib
import json
from urllib.parse import quote

from backend.core.update.manifest import ManifestVerifier, TARGETS, MAX_MANIFEST_BYTES, MAX_SIGNATURE_BYTES
from backend.core.update.version import Version
from .state import PublishError


def native_source(git, oid, version):
    try:
        name = oid + ":packaging/product.json"
        size = int(git.binary(["cat-file", "-s", name]))
        if not 0 < size <= 128*1024: return False
        product = json.loads(git.binary(["cat-file", "blob", name]))
        return isinstance(product,dict) and product.get("product_id") == "gitgo-terminal" and product.get("version") == version.removeprefix("v")
    except (PublishError,ValueError,TypeError):
        return False


def prepare_native_plan(storage, target, package, *, key_id, upgrade_route="direct"):
    from backend.remote.publish_accounts import resolve_secret
    from backend.remote.publish_platform import PlatformClient
    from .release_signing_keys import descriptor
    from .release_transport import ReleaseTransport
    if target.kind != "github" or target.instance != "https://github.com":
        raise PublishError("PUBLISH_NATIVE_RECIPE_UNSUPPORTED")
    if upgrade_route not in {"direct","legacy-repair"}: raise PublishError("PUBLISH_NATIVE_RECIPE_INVALID")
    key = descriptor(storage,target,key_id)
    public = base64.b64decode(key["ed25519_base64"],validate=True)
    verifier = ManifestVerifier({key_id:public})
    channel = "preview" if Version.parse(package["version"].removeprefix("v")).prerelease else "stable"
    tag = "updates-"+channel
    baselines, identities, sequences = {}, set(), set()
    with PlatformClient(target,resolve_secret(target)) as client:
        client.repository(); client.verify_identity()
        transport = ReleaseTransport(client)
        feed = transport.release(tag)
        if not feed or feed["draft"] is not False: raise PublishError("PUBLISH_FEED_PUBLIC_BASELINE_REQUIRED")
        inventory = transport.asset_inventory(feed)
        for platform,architecture in sorted(TARGETS):
            name = platform+"-"+architecture
            raw = transport.download(feed,tag,name+".manifest.json",maximum=MAX_MANIFEST_BYTES)
            sig = transport.download(feed,tag,name+".manifest.sig",maximum=MAX_SIGNATURE_BYTES)
            if raw is None or sig is None: raise PublishError("PUBLISH_FEED_PUBLIC_BASELINE_REQUIRED")
            manifest = verifier.verify(raw,sig)
            if ((manifest.platform,manifest.architecture)!=(platform,architecture) or manifest.product_id!="gitgo-terminal"
                    or manifest.channel!=channel or not manifest.url.startswith(target.instance+"/"+target.repository+"/releases/download/"+quote(manifest.release_id,safe="")+"/")):
                raise PublishError("PUBLISH_FEED_BASELINE_SCOPE_MISMATCH")
            identities.add((manifest.release_id,manifest.version,manifest.source_commit,manifest.source_tree))
            sequences.add(manifest.sequence)
            values={}
            for suffix,wire in ((".manifest.sig",sig),(".manifest.json",raw)):
                asset_name=name+suffix
                values[asset_name]={"id":inventory[asset_name]["id"],"bytes":len(wire),"sha256":hashlib.sha256(wire).hexdigest()}
            baselines[name]={"manifest":base64.b64encode(raw).decode("ascii"),"signature":base64.b64encode(sig).decode("ascii"),"assets":values}
    if len(identities)!=1 or len(sequences)!=1:
        raise PublishError("PUBLISH_FEED_BASELINE_INCONSISTENT")
    return {"schema":1,"channel":channel,"sequence":next(iter(sequences))+1,"signing":key,
            "upgrade_route":upgrade_route,"targets":sorted(baselines),"baselines":baselines,"feed_release_id":feed["id"]}
