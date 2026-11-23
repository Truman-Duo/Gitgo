"""Real project CAS/crypto/contracts with disposable HTTP effects; no OS packaging proof."""
import base64
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import io
import json
import tarfile
import zipfile

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat,PrivateFormat,NoEncryption

from tests.test_publish_commands import setup,call,git
from tests.test_publish_formal import formal_setup,prepare,command,protocol
from tests.test_product_release_controller import remote,latest,settle,run
from tests.test_publish_release_transport import Platform
from tests.test_update_transaction import build_fixture,refresh_build,compatibility_wire
from tests.test_update_manifest import manifest_wire,sign_raw
from backend.core.update.manifest import TARGETS,ManifestVerifier
from backend.core.publish.release_signing import github_policy


class NativeRemote(Platform):
    def __init__(self,package,archives,key):
        super().__init__("github")
        self.package,self.feed,self.downloads,self.feed_writes,self.fail_delete=package,{},0,[],False
        now=datetime.now(timezone.utc).replace(microsecond=0)
        self.next=1000
        for platform,architecture in sorted(TARGETS):
            target=platform+"-"+architecture
            wire=manifest_wire(platform=platform,architecture=architecture,archive_format="zip" if platform=="windows" else "tar.gz",
                version="0.9.0",channel="stable",sequence=8,release_id="v0.9.0",minimum_updater_version="0.2.0",
                url="https://github.com/team/repo/releases/download/v0.9.0/"+target+(".zip" if platform=="windows" else ".tar.gz"),
                published_at=(now-timedelta(days=2)).isoformat().replace("+00:00","Z"),expires_at=(now-timedelta(days=1)).isoformat().replace("+00:00","Z"),data_compatibility=compatibility_wire())
            raw=json.dumps(wire).encode()
            self.put(target+".manifest.json",raw);self.put(target+".manifest.sig",sign_raw(raw,key,key_id="test-publisher"))
        source=io.BytesIO()
        with zipfile.ZipFile(source,"w") as bundle:
            for name,body in archives.items():bundle.writestr(name,body)
        self.body=source.getvalue()
        self.run_row={"id":10,"run_attempt":2,"head_sha":package["source_oid"],"repository":{"id":20},"head_repository":{"id":20},
            "workflow_id":30,"path":".github/workflows/build.yml","status":"completed","conclusion":"success","run_started_at":"2026-11-08T00:00:00Z"}
        self.artifact={"id":40,"name":"native-builds","size_in_bytes":len(self.body),"digest":"sha256:"+hashlib.sha256(self.body).hexdigest(),
            "expired":False,"created_at":"2026-11-08T00:01:00Z","workflow_run":{"id":10,"repository_id":20,"head_repository_id":20,"head_sha":package["source_oid"]}}
    def put(self,name,body):
        self.next+=1;self.feed[name]={"id":self.next,"body":body}
    def request(self,request):
        path,base=request.url.path,self.target.repo_path
        if path in {base+"/actions/runs/10",base+"/actions/runs/10/attempts/2"}:return httpx.Response(200,json=self.run_row)
        if path==base+"/actions/artifacts/40":return httpx.Response(200,json=self.artifact)
        if path==base+"/actions/runs/10/attempts/2/jobs":return httpx.Response(200,json={"jobs":[{
            "id":50,"run_id":10,"run_attempt":2,"name":"CI gate","status":"completed","conclusion":"success"}]})
        if path==base+"/actions/artifacts/40/zip":self.downloads+=1;return httpx.Response(200,content=self.body)
        if path==base+"/releases/tags/updates-stable":return httpx.Response(200,json={"id":900,"tag_name":"updates-stable","body":"channel","draft":False})
        if path==base+"/releases/900/assets":
            if request.method=="POST":
                name=request.url.params["name"];assert name not in self.feed
                self.put(name,request.content);self.feed_writes.append(("upload",name))
                return httpx.Response(201,json={"id":self.next})
            return httpx.Response(200,json=[{"id":value["id"],"name":name,"state":"uploaded","size":len(value["body"]),
                "digest":"sha256:"+hashlib.sha256(value["body"]).hexdigest()} for name,value in self.feed.items()])
        if path.startswith(base+"/releases/assets/"):
            identifier=int(path.rsplit("/",1)[-1]);found=next(((name,row) for name,row in self.feed.items() if row["id"]==identifier),None)
            if found:
                name,row=found
                if request.method=="DELETE":
                    del self.feed[name];self.feed_writes.append(("delete",name))
                    if self.fail_delete:self.fail_delete=False;raise httpx.ReadTimeout("lost deletion response",request=request)
                    return httpx.Response(204)
                return httpx.Response(200,content=row["body"])
        return super().request(request)


@pytest.fixture
def native_release(formal_setup,monkeypatch,tmp_path_factory):
    context=formal_setup
    root=context[2]
    (root/"packaging").mkdir();(root/"packaging/product.json").write_text(json.dumps({"product_id":"gitgo-terminal","version":"1.0.0"}))
    git(root,"add","packaging/product.json");git(root,"commit","-m","native product declaration")
    protocol(monkeypatch)
    item=prepare(context);item=command(context,"verify",item)["item"];item=command(context,"execute",item)["item"]
    packaged=call(context,"package","native-package",item_id=item["id"],expected_revision=item["revision"],version="1.0.0")
    assert "item" in packaged,packaged
    item,package_id=packaged["item"],packaged["package_id"];package=item["packages"][0]
    key=Ed25519PrivateKey.generate();public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
    archives={}
    for platform,architecture in sorted(TARGETS):
        name=platform+"-"+architecture;stage=tmp_path_factory/(name+"-stage")
        build_fixture(stage,version="1.0.0",platform=platform,architecture=architecture)
        (stage/"release-trust.json").write_text(json.dumps(github_policy(public,"test-publisher",platform,architecture,"team/repo")))
        refresh_build(stage,version="1.0.0",platform=platform,architecture=architecture)
        build=json.loads((stage/"build-manifest.json").read_bytes());build["source"]={"commit":package["source_oid"],"tree":package["tree_oid"],"dirty":False}
        (stage/"build-manifest.json").write_text(json.dumps(build))
        body=io.BytesIO()
        if platform=="windows":
            with zipfile.ZipFile(body,"w") as bundle:
                for path in stage.rglob("*"):
                    if path.is_file():bundle.write(path,path.relative_to(stage).as_posix())
        else:
            def modes(info):
                info.mode=0o700 if info.isdir() or info.name.removeprefix(name+"/") in {"gitgo","internal/gitgo-host/gitgo-host","install.sh","uninstall.sh"} else 0o600
                return info
            with tarfile.open(fileobj=body,mode="w:gz") as bundle:bundle.add(stage,arcname=name,filter=modes)
        archives[name+(".zip" if platform=="windows" else ".tar.gz")]=body.getvalue()
    platform=NativeRemote(package,archives,key);remote(monkeypatch,package,route=platform.request)
    secret=base64.b64encode(key.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())).decode()
    saved=call(context,"signing.save","key-settings",target=platform.target.descriptor(),key_id="test-publisher",token=secret)
    assert saved["effect_state"]=="committed"
    result=call(context,"release.plan","native-plan",item_id=item["id"],expected_revision=item["revision"],package_id=package_id,
        target=platform.target.descriptor(),ci_artifacts=[{"run_id":10,"attempt":2,"artifact_id":40}],delivery_recipe="gitgo.native",signing_key_id="test-publisher")
    assert "item" in result,result
    try:yield context,result["item"],platform,key,secret
    finally:context[0].publish_runtime.close()


def test_product_native_recipe_signs_real_four_target_contracts_reuses_and_verifies_public_delivery(native_release,monkeypatch):
    context,item,platform,key,secret=native_release
    before=dict(platform.feed)
    run(context,item,request="native-prepare");item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"]=="completed",item["release_controller"]
    assert platform.feed==before and platform.release is None
    run(context,item,"published",request="native-publish");item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"]=="completed",item["release_controller"]
    assert platform.downloads==1 and len(platform.feed_writes)==16 and len(set(platform.feed_writes))==16
    verifier=ManifestVerifier({"test-publisher":key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)})
    for target in [p+"-"+a for p,a in TARGETS]:
        manifest=verifier.verify(platform.feed[target+".manifest.json"]["body"],platform.feed[target+".manifest.sig"]["body"])
        assert manifest.sequence==9 and manifest.source_commit==item["packages"][0]["source_oid"]
    from backend.core.publish import public_delivery
    fetch=public_delivery.download_fact
    public_requests=[]
    def public(request):
        public_requests.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        parts=request.url.path.split("/");tag,name=parts[-2:]
        raw=platform.feed[name]["body"] if tag=="updates-stable" else platform.assets[name]
        return httpx.Response(200,content=raw)
    def download(url,expected,**kwargs):
        with httpx.Client(transport=httpx.MockTransport(public)) as client:return fetch(url,expected,client=client,**kwargs)
    monkeypatch.setattr(public_delivery,"download_fact",download)
    writes=list(platform.writes),list(platform.feed_writes)
    run(context,item,"verified",request="native-public");item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"]=="completed",item["release_controller"]
    assert item["release_controller"]["job"]["feed_state"]=="advanced" and len(public_requests)>8
    timing=item["release_controller"]["job"]
    assert timing["started_at"]<=timing["finished_at"] and timing["elapsed_seconds"]>=0 and timing["queue_seconds"]>=0
    assert (platform.writes,platform.feed_writes)==writes and platform.downloads==1
    status=context[0].invoke("publish.release.status",{"project":context[1].name,"item_id":item["id"],"limit":100})
    assert status["plan"]["native"]["sequence"]==9 and status["plan"]["capabilities"]["signed_update_feed"] is True
    assert secret not in json.dumps(status) and "PRIVATE KEY" not in json.dumps(status)


def test_product_native_feed_response_loss_requires_readonly_reconciliation_then_exact_resume(native_release,monkeypatch):
    context,item,platform,_,_=native_release
    platform.fail_delete=True
    run(context,item,"published",request="native-loss");item=settle(context,item["id"])
    job=item["release_controller"]["job"]
    assert job["state"]=="needs_reconciliation",job
    writes=list(platform.feed_writes)
    # Later CI/source/freshness changes cannot block observation of the
    # already-recorded effect, nor authorize any new write.
    import backend.core.publish.native_release_recipe as native_module
    actual_clock=native_module.datetime
    class ExpiredClock:
        @staticmethod
        def now(tz):return actual_clock.now(tz)+timedelta(days=8)
    monkeypatch.setattr(native_module,"datetime",ExpiredClock)
    platform.run_row["run_attempt"]=3
    git(context[2],"update-ref","refs/heads/formal",git(context[2],"rev-parse","HEAD"))
    recovered=call(context,"release.reconcile","native-observe",item_id=item["id"],expected_revision=item["revision"],job_id=job["id"])
    assert "item" in recovered,recovered
    item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"]=="blocked" and platform.feed_writes==writes
    assert item["release_controller"]["job"]["phases"]["draft"]["state"]=="observed"
    run(context,item,"published",request="native-still-stale");item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"]=="failed" and platform.feed_writes==writes
    git(context[2],"update-ref","refs/heads/formal",item["packages"][0]["source_oid"])
    platform.run_row["run_attempt"]=2
    monkeypatch.setattr(native_module,"datetime",actual_clock)
    run(context,item,"published",request="native-resume");item=settle(context,item["id"])
    assert item["release_controller"]["job"]["state"]=="completed",item["release_controller"]
    assert len(platform.feed_writes)==16 and len(set(platform.feed_writes))==16 and platform.downloads==1
