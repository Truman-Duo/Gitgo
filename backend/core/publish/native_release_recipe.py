"""Gitgo's four-target archive/signing/feed recipe on product-owned adapters."""
import base64
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace

from backend.core.update.manifest import ManifestVerifier, TARGETS
from .ci_release_recipe import CIReleaseRecipe
from .release_archive import ArchiveEvidence
from .release_artifacts import ArtifactContent
from .release_dag import run_dag
from .release_engine import ReleaseBlocked
from .release_feed import advance_feed
from .release_signing import prepare as sign
from .state import PublishError, digest, encoded


class _Archive:
    def __init__(self, store, asset): self.store,self.asset,self.name=store,asset,asset["name"]
    def open(self, mode):
        if mode!="rb": raise ValueError("Read-only archive")
        return self.store.reader(self.asset)
    def stat(self): return SimpleNamespace(st_size=self.asset["size"])


class NativeReleaseRecipe(CIReleaseRecipe):
    identity = {"id":"gitgo.native-release", "version":2}

    def __init__(self, *args, native, signing_key, **kwargs):
        super().__init__(*args, **kwargs)
        self.native,self.signing_key=native,signing_key
        self.version="v"+self.package["version"].removeprefix("v")
        self.verifier=ManifestVerifier({native["signing"]["key_id"]:base64.b64decode(native["signing"]["ed25519_base64"],validate=True)})
        self.ready=None
        self.record.setdefault("feed_baseline", {"release_id":native["feed_release_id"],"assets":{
            name:fact["id"] for baseline in native["baselines"].values() for name,fact in baseline["assets"].items()}})

    def _raw(self, asset, limit=128*1024): return self.artifacts.bytes(asset,limit)

    def _verify_signed(self):
        if self.ready is None: return
        by_name={asset["name"]:asset for asset in self.ready["generated"]}
        archives={asset["name"]:asset for asset in self.ready["archives"]}
        for name in self.native["targets"]:
            manifest=self.verifier.verify(self._raw(by_name[name+".manifest.json"]),self._raw(by_name[name+".manifest.sig"],1024))
            if (manifest.source_commit!=self.oid or manifest.source_tree!=self.package["tree_oid"]
                    or manifest.sequence!=self.native["sequence"] or manifest.channel!=self.native["channel"]
                    or manifest.release_id!=self.version or manifest.version!=self.package["version"].removeprefix("v")):
                raise PublishError("PUBLISH_NATIVE_SIGNATURE_INPUT_CHANGED")
            if not self.observe_only and manifest.expires_at <= datetime.now(timezone.utc):
                raise PublishError("PUBLISH_SIGNED_MANIFEST_EXPIRED")
            from urllib.parse import unquote,urlsplit
            archive=archives.get(unquote(urlsplit(manifest.url).path.rsplit("/",1)[-1]))
            if not archive or archive["size"]!=manifest.bytes or archive["sha256"]!=manifest.sha256:
                raise PublishError("PUBLISH_NATIVE_SIGNATURE_INPUT_CHANGED")

    def guard(self, context, prepared):
        super().guard(context,prepared)
        self._verify_signed()

    def prepare(self, context):
        super().prepare(context)
        inputs=sorted([{key:asset[key] for key in ("name","size","sha256")} for asset in self.assets],key=lambda row:row["name"])
        binding=digest({"recipe":self.identity,"native":self.native,"source":self.oid,"tree":self.package["tree_oid"],"inputs":inputs})
        previous=self.journal.native_prepared()
        if previous and previous.get("binding")==binding:
            for asset in previous["generated"]: self.artifacts.verify(asset)
            self.ready=previous
            self.journal.record("signed-assets","reused",reason="PUBLISH_RELEASE_INPUTS_UNCHANGED")
        else:
            if self.observe_only: raise ReleaseBlocked("prepared","PUBLISH_NATIVE_PREPARATION_REQUIRED")
            if self.record.get("release_id") or self.record.get("current"):
                raise PublishError("PUBLISH_NATIVE_PREPARATION_UNAVAILABLE")
            key=self.signing_key()
            candidates=[asset for asset in self.assets if asset["name"].endswith((".zip",".tar.gz")) and asset not in self.fixed_assets]
            inspected={}
            def inspect(step, dependencies):
                asset=candidates[step["index"]]
                with tempfile.TemporaryDirectory(prefix="gitgo-release-inspection-") as temporary, self.artifacts.reader(asset) as source:
                    evidence=ArchiveEvidence(asset["name"],Path(temporary),source=source,size=asset["size"],cancelled=context.cancelled)
                    product,build=evidence.metadata("product.json"),evidence.metadata("build-manifest.json")
                    target=product.get("platform","")+"-"+product.get("architecture","")
                    if (target not in self.native["targets"] or product.get("product_id")!="gitgo-terminal"
                            or product.get("version")!=self.package["version"].removeprefix("v")
                            or build.get("source")!={"commit":self.oid,"tree":self.package["tree_oid"],"dirty":False}
                            or any(row.get("path","").lower().endswith((".bat",".cmd")) for row in build.get("files",[]))):
                        raise PublishError("PUBLISH_NATIVE_ARCHIVE_IDENTITY_MISMATCH")
                    baseline=self.native["baselines"][target]
                    raw,sig,_=sign(None,_Archive(self.artifacts,asset),key,self.native["signing"]["key_id"],self.native["channel"],
                        self.native["sequence"],self.transport.target.repository,self.version,
                        baseline_manifest=base64.b64decode(baseline["manifest"],validate=True),
                        baseline_signature=base64.b64decode(baseline["signature"],validate=True),
                        upgrade_route=self.native["upgrade_route"],archive_evidence=evidence)
                generated=[]
                for name,wire in ((target+".manifest.json",raw),(target+".manifest.sig",sig),
                                  (asset["name"]+".sha256",(asset["sha256"]+"  "+asset["name"]+"\n").encode("ascii"))):
                    value=self.artifacts.ingest((wire[offset:offset+65536] for offset in range(0,len(wire),65536)),size=len(wire))
                    generated.append({"name":name,**value})
                return {"target":target,"archive":{key:asset[key] for key in asset if key!="raw"},"generated":generated}
            if len(candidates)!=len(TARGETS): raise PublishError("PUBLISH_NATIVE_FOUR_ARCHIVES_REQUIRED")
            results=run_dag([{"id":"target-"+str(index),"index":index} for index in range(len(candidates))],inspect,
                            workers=context.workers,cancelled=context.cancelled or (lambda:False))
            for value in results.values():
                if value["target"] in inspected: raise PublishError("PUBLISH_NATIVE_TARGET_DUPLICATE")
                inspected[value["target"]]=value
            if set(inspected)!=set(self.native["targets"]): raise PublishError("PUBLISH_NATIVE_FOUR_ARCHIVES_REQUIRED")
            generated=[asset for target in sorted(inspected) for asset in inspected[target]["generated"]]
            summary=encoded({"schema_version":1,"version":self.version,"source_oid":self.oid,"tree_oid":self.package["tree_oid"],
                "targets":sorted(inspected),"sequence":self.native["sequence"],"inventory_and_contract_verified":True,
                "formal_terminal_verified":False,"future_upgrade_verified":False})
            generated.append({"name":"validation-summary.json",**self.artifacts.ingest([summary],size=len(summary))})
            self.ready={"binding":binding,"generated":generated,"archives":[inspected[target]["archive"] for target in sorted(inspected)]}
            references=[]
            for asset in generated: references.extend(self.artifacts.references(asset))
            self.journal.save_native_prepared(self.ready,references)
            self.journal.record("signed-assets","success",targets=sorted(inspected),sequence=self.native["sequence"])
        self.assets=self.fixed_assets+[{**asset,"raw":ArtifactContent(self.artifacts,asset)} for asset in self.ready["archives"]+self.ready["generated"]]
        self.guard(context,None)
        return {"source_oid":self.oid,"assets":len(self.assets),"signed_targets":self.native["targets"],"sequence":self.native["sequence"]}

    def publish(self, context, prepared, draft):
        super().publish(context,prepared,draft)
        # Uploads may take time: re-observe candidate, account and CI once at
        # the feed boundary. Each following effect retains the same journal
        # lease, exact plan binding and durable pre-write intent.
        self.validate(context)
        recipe=self
        wanted={}
        by_name={asset["name"]:asset for asset in self.assets}
        for target in self.native["targets"]:
            for suffix in (".manifest.sig",".manifest.json"):
                name=target+suffix; asset=by_name[name]
                wanted[name]={"asset":asset,"new":{"bytes":asset["size"],"sha256":asset["sha256"]},
                              "old":{key:self.native["baselines"][target]["assets"][name][key] for key in ("bytes","sha256")}}
        class Remote:
            def release(self,tag): return recipe.transport.release(tag)
            def assets(self,row):
                found=recipe.transport.asset_inventory(row)
                for name in wanted:
                    value=found.get(name)
                    if value and value.get("digest") is None:
                        raw=recipe.transport.download(row,"updates-"+recipe.native["channel"],name,maximum=128*1024)
                        value.update(size=len(raw),digest="sha256:"+hashlib.sha256(raw).hexdigest())
                return found
            def delete(self,identifier): return recipe.transport.delete_asset(identifier)
            def upload(self,row,asset): return recipe.transport.upload(row,"updates-"+recipe.native["channel"],asset,asset["raw"])
        class Journal:
            def baseline(self): return recipe.record["feed_baseline"]
            def save_baseline(self,value): recipe._save(feed_baseline=value)
            def delete_recorded(self,name):
                return recipe.record.get("feed_delete_intents",{}).get(name)==recipe.record["feed_baseline"]["assets"].get(name)
            def write(self,name,action,**intent):
                recipe._check_cancel(True)
                if name.startswith("feed-delete-"):
                    recipe.record.setdefault("feed_delete_intents",{})[name[len("feed-delete-"):]]=intent["asset_id"]
                recipe._write(name,action)
        result=advance_feed(Remote(),"updates-"+self.native["channel"],wanted,Journal(),observe_only=self.observe_only)
        self._save(feed_state="advanced",current=None)
        self.journal.record("feeds","success",**result)

    def verify(self, context, prepared):
        super().verify(context,prepared)
        from .public_delivery import verify_assets
        names={target+suffix for target in self.native["targets"] for suffix in (".manifest.json",".manifest.sig")}
        feed=[asset for asset in self.assets if asset["name"] in names]
        facts=verify_assets(self.transport.target,"updates-"+self.native["channel"],feed,
                            cancelled=context.cancelled,progress=self.transport.progress)
        self._save(public_feed_delivery=facts,current=None)

    def result(self, phase, draft):
        return {**super().result(phase,draft),"signed_assets_prepared":self.ready is not None,
                "feed_advanced":self.record.get("feed_state")=="advanced",
                "native_managed_install_verified":False,"formal_terminal_verified":False}
