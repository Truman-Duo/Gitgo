"""Isolated reference fixture. No real accounts, remote forge or product state."""
from __future__ import annotations
import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from publish_features import PublishFeatures
from workflow import ThreeRepoWorkflow

ROOT = Path(__file__).resolve().parent
ACTORS = {"duo": {"name": "Duo", "role": "maintainer"}, "lin": {"name": "Lin", "role": "contributor"}, "chen": {"name": "Chen", "role": "contributor"}, "guest": {"name": "Visitor", "role": "viewer"}}
PROVIDERS = {
    "github": {"label": "GitHub", "mode": "contract-simulation", "issues": True, "review": True, "api": "https://api.github.com", "web": "https://github.com/demo/atlas", "git": "https://github.com/demo/atlas.git"},
    "gitlab": {"label": "GitLab · 自建", "mode": "contract-simulation", "issues": True, "review": True, "api": "https://forge.example.test/gitlab/api/v4", "web": "https://forge.example.test/gitlab/team/atlas", "git": "ssh://git@forge.example.test:2222/team/atlas.git"},
    "bare": {"label": "Bare Git", "mode": "real-local", "issues": False, "review": False, "api": "", "web": "", "git": "local fixture repository"},
}

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def encode(v): return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
def digest(data): return hashlib.sha256(data).hexdigest()
def uid(prefix): return prefix + uuid.uuid4().hex[:10]

class Problem(Exception):
    def __init__(self, code, message, status=409, details=None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details

def require(ok, code, message, status=409, details=None):
    if not ok: raise Problem(code, message, status, details)

class Demo(PublishFeatures, ThreeRepoWorkflow):
    require = staticmethod(require)
    def __init__(self, data_root):
        self.root = Path(data_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "state.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL); CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, actor TEXT, fingerprint TEXT, body TEXT, created TEXT);")
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        if row: self.s = json.loads(row[0])
        else:
            self.s = self.seed()
            self.save()
        self.ensure_publish()
        self.ensure_workflow()
        self.save()

    @property
    def repo(self): return self.root / self.s["session"] / "repository.git"

    def git_at(self, repo, *args, data=None, check=True, env_extra=None):
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Gitgo Demo", "GIT_AUTHOR_EMAIL": "demo@example.invalid", "GIT_COMMITTER_NAME": "Gitgo Demo", "GIT_COMMITTER_EMAIL": "demo@example.invalid", **(env_extra or {})}
        result = subprocess.run(["git", "--git-dir", str(repo), "-c", "core.hooksPath=" + os.devnull, *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=25)
        if check and result.returncode:
            raise Problem("GIT_FAILED", result.stderr.decode("utf-8", "replace").strip() or result.stdout.decode("utf-8", "replace")[:1600], 422)
        return result

    def git(self, *args, **kw): return self.git_at(self.repo, *args, **kw)
    def text(self, *args): return self.git(*args).stdout.decode("utf-8", "replace").strip()

    def make_commit(self, repo, files, parents, message, author="Gitgo Demo"):
        # Construct a tree from owned fixture content without checking out the user's repository.
        entries = []
        for path, body in sorted(files.items()):
            blob = self.git_at(repo, "hash-object", "-w", "--stdin", data=body.encode()).stdout.decode().strip()
            entries.append((path, blob))
        def tree(prefix=""):
            rows, directories = [], set()
            for path, blob in entries:
                if not path.startswith(prefix): continue
                tail = path[len(prefix):]
                if "/" in tail: directories.add(tail.split("/")[0])
                else: rows.append(f"100644 blob {blob}\t{tail}\n")
            for name in sorted(directories): rows.append(f"040000 tree {tree(prefix + name + '/')}\t{name}\n")
            return self.git_at(repo, "mktree", data="".join(rows).encode()).stdout.decode().strip()
        args = ["commit-tree", tree()]
        for parent in parents: args += ["-p", parent]
        return self.git_at(repo, *args, data=message.encode(), env_extra={"GIT_AUTHOR_NAME": author}).stdout.decode().strip()

    def seed(self):
        session = "lab-" + uuid.uuid4().hex[:12]
        folder = self.root / session
        folder.mkdir()
        repo = folder / "repository.git"
        result = subprocess.run(["git", "init", "--bare", "--initial-branch=master", "--template=", str(repo)], capture_output=True, timeout=15)
        require(result.returncode == 0, "GIT_INIT", result.stderr.decode("utf-8", "replace"), 500)
        base = {"README.md": "# Atlas\n\nA fictional offline delivery toolkit for the Gitgo publish demo.\n", "docs/usage.md": "# Usage\n\nDownload a package, verify its digest, then unpack.\n", "test_core.py": "import unittest\nclass Core(unittest.TestCase):\n def test_version(self): self.assertEqual('0.8'.split('.')[0], '0')\n"}
        root = self.make_commit(repo, base, [], "init: create Atlas delivery toolkit")
        base["VERSION"] = "0.8.0-preview\n"
        master = self.make_commit(repo, base, [root], "release: prepare 0.8 preview baseline")
        a = {**base, "download.py": "def normalize_name(name):\n    return name.strip().replace(' ', '-')\n", "test_download.py": "import unittest\nfrom download import normalize_name\nclass Download(unittest.TestCase):\n def test_name(self): self.assertEqual(normalize_name(' atlas kit '), 'atlas-kit')\n def test_empty(self):\n  with self.assertRaises(ValueError): normalize_name('  ')\n"}
        a2 = self.make_commit(repo, a, [master], "feat: add offline package naming", "Lin")
        a["docs/usage.md"] += "\nOffline download is available. Empty names still need handling.\n"
        a3 = self.make_commit(repo, a, [a2], "docs: describe offline package flow [A3]", "Lin")
        b = {**base, "integrity.py": "import hashlib\ndef checksum(data):\n    return hashlib.sha256(data).hexdigest()\n", "test_integrity.py": "import unittest\nfrom integrity import checksum\nclass Integrity(unittest.TestCase):\n def test_digest(self): self.assertEqual(checksum(b'abc'), 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')\n"}
        b2 = self.make_commit(repo, b, [master], "feat: verify package SHA-256 [B2]", "Chen")
        for ref, oid in [("master", master), ("feature/offline", a3), ("feature/checksum", b2)]: self.git_at(repo, "update-ref", "refs/heads/" + ref, oid)
        for label, oid in [("a3", a3), ("b2", b2)]: self.git_at(repo, "update-ref", "refs/demo/candidates/" + label, oid)
        return {"schema": 1, "session": session, "revision": 1, "created": now(), "provider": "github", "config": {"prefix": "GG", "privacy": True, "api": PROVIDERS["github"]["api"], "web": PROVIDERS["github"]["web"], "git": PROVIDERS["github"]["git"], "registry": "local://team-packages"}, "issues": [{"id": 42, "title": "打通离线包分发与失败重试", "body": "离线下载已有基础。还需要空文件名边界、摘要核验和组合验证。先接入 integration/42，由 Chen 衔接；Lin 继续补齐边界。", "author": "lin", "owner": "chen", "state": "open", "version": 1, "comments": [{"id": "comment-seed-1", "author": "lin", "body": "A3 可以下载了。空文件名这一处还没处理，希望先确认方向。", "candidate": a3, "anchor": None, "version": 1, "time": now()}, {"id": "comment-seed-2", "author": "duo", "body": "这份实现可以作为基础。先集成，剩余修改继续在这里追踪；进入 master 前再检查整体。", "candidate": a3, "anchor": None, "version": 1, "time": now()}]}], "proposals": [{"id": "p17", "number": 17, "title": "离线下载与文件名归一化", "author": "lin", "branch": "feature/offline", "candidate": a3, "base": master, "label": "A3", "history": [{"label": "A3", "oid": a3}], "issue": 42, "review": "needs-work", "integrations": []}, {"id": "p18", "number": 18, "title": "安装包 SHA-256 校验", "author": "chen", "branch": "feature/checksum", "candidate": b2, "base": master, "label": "B2", "history": [{"label": "B2", "oid": b2}], "issue": 42, "review": "pending", "integrations": []}], "branches": {"master": {"owner": "duo", "purpose": "Formal", "retention": "正式发布线", "deleted": False}, "feature/offline": {"owner": "lin", "purpose": "Workspace", "retention": "", "deleted": False}, "feature/checksum": {"owner": "chen", "purpose": "Workspace", "retention": "", "deleted": False}}, "checks": {}, "builds": [], "releases": [], "channels": {}, "integrations": [], "events": [{"id": "seed", "time": now(), "actor": "system", "action": "demo.seed", "summary": "已创建隔离 Git 仓库 · A3 / B2 / master", "revision": 1}], "fault": "none"}

    def save(self):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (encode(self.s),))
        self.db.commit()

    def event(self, actor, action, summary, details=None):
        self.s["events"].append({"id": uid("evt-"), "time": now(), "actor": actor, "action": action, "summary": summary, "details": details, "revision": self.s["revision"] + 1})

    def oid(self, ref):
        r = self.git("rev-parse", "--verify", ref, check=False)
        return r.stdout.decode().strip() if r.returncode == 0 else None

    def branch_name(self, name):
        require(isinstance(name, str) and 1 <= len(name) <= 100 and not name.startswith("-"), "BAD_REF", "分支名称无效", 422)
        require(self.git("check-ref-format", "refs/heads/" + name, check=False).returncode == 0, "BAD_REF", "分支名称无效", 422)
        return name

    def proposal(self, pid):
        p = next((p for p in self.s["proposals"] if p["id"] == pid), None)
        require(p, "NOT_FOUND", "候选不存在", 404)
        return p

    def issue(self, iid):
        i = next((i for i in self.s["issues"] if i["id"] == int(iid)), None)
        require(i, "NOT_FOUND", "事项不存在", 404)
        return i

    def maintainer(self, actor): require(ACTORS[actor]["role"] == "maintainer", "FORBIDDEN", "当前演示账户无维护者权限；切换视角不会扩大权限", 403)
    def writer(self, actor): require(ACTORS[actor]["role"] != "viewer", "FORBIDDEN", "访客只有读取权限", 403)
    def collaboration(self): require(PROVIDERS[self.s["provider"]]["issues"], "UNSUPPORTED", "Bare Git 不提供 Issue / 评论；请配置协作适配器", 422)

    def files(self, oid):
        raw = self.git("archive", "--format=zip", oid).stdout
        with zipfile.ZipFile(io.BytesIO(raw)) as z: return {n: z.read(n).decode() for n in z.namelist() if not n.endswith("/")}

    def check(self, oid):
        # Execute only our private fixture, in an owned extraction directory. Not a general CI runner.
        folder = self.root / self.s["session"] / "checks" / uid("run-")
        folder.mkdir(parents=True)
        for name, body in self.files(oid).items():
            path = (folder / name).resolve()
            require(path.is_relative_to(folder.resolve()), "UNSAFE_PATH", "构建路径超出隔离目录", 422)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-v"], cwd=folder, capture_output=True, timeout=20, env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(folder)})
        return {"oid": oid, "state": "passed" if result.returncode == 0 else "failed", "exit_code": result.returncode, "time": now(), "recipe": "fixture-unittest/v1", "log": (result.stdout + result.stderr).decode("utf-8", "replace")[-12000:]}

    def graph(self):
        lines = self.text("log", "--all", "--topo-order", "--format=%H%x09%P%x09%s%x09%an", "-100").splitlines()
        refs = {}
        for line in self.text("for-each-ref", "--format=%(objectname)%09%(refname)", "refs/heads", "refs/tags").splitlines():
            oid, ref = line.split("\t", 1)
            refs.setdefault(oid, []).append(ref.replace("refs/heads/", "").replace("refs/tags/", "tag: "))
        return [{"oid": bits[0], "parents": bits[1].split(), "subject": bits[2], "author": bits[3], "refs": refs.get(bits[0], [])} for line in lines if len(bits := line.split("\t", 3)) == 4]

    def snapshot(self, actor):
        with self.lock:
            out = copy.deepcopy(self.s)
            out['workflow'] = self.flow_snapshot()
            out["actors"], out["actor"], out["providers"] = ACTORS, actor, PROVIDERS
            out["permissions"] = {"write": ACTORS[actor]["role"] != "viewer", "maintain": ACTORS[actor]["role"] == "maintainer"}
            out["graph"] = self.graph()
            raw_graph = self.git("log", "--all", "--graph", "--topo-order", "--color=never", "--format=%x1e%H", "-100").stdout.decode()
            out["graph_lines"] = [{"prefix": line.split("\x1e")[0], "oid": line.split("\x1e")[1] if "\x1e" in line else None} for line in raw_graph.split("\n") if line]
            for name, branch in out["branches"].items(): branch["tip"] = self.oid("refs/heads/" + name)
            out["backend"] = {"mode": "local-executable-demo", "git": True, "database": "SQLite · single local authority", "external_network": False, "auth": "selectable fixture identities, not production authentication"}
            return out

    def integration_preview(self, actor, a):
        self.maintainer(actor)
        p = self.proposal(a["proposal"])
        require(a.get("source") == p["candidate"], "STALE_CANDIDATE", "候选已有新版本，请重新预览")
        target = self.branch_name(a["target"])
        method = a.get("method", "merge")
        require(method in ["merge", "squash", "ff", "branch"], "UNSUPPORTED", "当前适配器不支持此合并方法", 422)
        old = self.oid("refs/heads/" + target)
        require(target != p["branch"], "SAME_REF", "来源与目标必须不同")
        if method == "branch": require(not old, "BRANCH_EXISTS", "仅创建分支要求一个尚不存在的分支名")
        if old: require(target in self.s["branches"], "UNMANAGED_REF", "目标尚未登记")
        base = old or self.oid("refs/heads/master")
        require(a.get("owner") in ACTORS and a["owner"] != "guest", "OWNER_REQUIRED", "请指定下一步负责人", 422)
        return {"proposal": p["id"], "source": p["candidate"], "source_label": p["label"], "target": target, "expected_target": old, "base": base, "method": method, "owner": a["owner"], "formal": target == "master", "issue": p["issue"], "meaning": "仅创建引用，不表示已合并" if method == "branch" else ("合入正式线；事项仍需明确关闭" if target == "master" else "接入集成线；不改变 master，不自动关闭事项")}

    def integrate(self, actor, a):
        plan = self.integration_preview(actor, a)
        require(a.get("expected_target") == plan["expected_target"], "TARGET_MOVED", "目标分支已移动，请重新预览")
        p, source, base = self.proposal(a["proposal"]), plan["source"], plan["base"]
        method, target = plan["method"], plan["target"]
        if method == "branch": result = source
        elif method == "ff":
            require(self.git("merge-base", "--is-ancestor", base, source, check=False).returncode == 0, "NOT_FAST_FORWARD", "目标与候选已经分叉，不能 fast-forward")
            result = source
        else:
            merge = self.git("merge-tree", "--write-tree", base, source, check=False)
            require(merge.returncode == 0, "MERGE_CONFLICT", "存在内容冲突；保留原分支，需在工作区解决后重新提交候选", 409, merge.stdout.decode("utf-8", "replace")[:3000])
            tree = merge.stdout.decode().splitlines()[0]
            parents = [base] + ([source] if method == "merge" and base != source else [])
            args = ["commit-tree", tree]
            for parent in parents: args += ["-p", parent]
            result = self.git(*args, data=f"{method}: {p['label']} → {target}; source {source}\n\nOriginal author: {ACTORS[p['author']]['name']}\n".encode()).stdout.decode().strip()
        if target == 'master':
            report, outbound = self.require_outbound(result)
            if method == 'ff':
                require(not report['excluded'], 'PROJECTION_REQUIRED', 'Fast-forward 不能改写树；有排除内容时请选择正式整理或 squash。', 422)
            else:
                message = a.get('message') or self.formatted(source)
                require(re.match(r'^\[[A-Z0-9]+-\d+\]\s+\w+', message), 'INVALID_MESSAGE', '正式提交格式无效', 422)
                result = self.make_commit(self.repo, outbound, [base] + ([source] if method == 'merge' else []), message)
        check = self.check(result)
        if target == "master":
            require(method != "branch", "PROTECTED", "正式线不能用 branch-only 操作替代")
            require(check["state"] == "passed", "CHECKS_FAILED", "组合检查未通过，master 未改变。可先接入集成分支继续完善。", 422, check)
        self.git("update-ref", "refs/heads/" + target, result, plan["expected_target"] or "0" * 40)
        self.s["checks"][result] = check
        self.s["branches"].setdefault(target, {"owner": plan["owner"], "purpose": "Trial", "retention": "", "deleted": False})
        rec = {"id": uid("int-"), **plan, "result": result, "time": now(), "state": "branch-created" if method == "branch" else "integrated", "check": check["state"]}
        self.s["integrations"].append(rec)
        p["integrations"].append(rec["id"])
        if target == 'master':
            self.s['triage'][source] = {'source':source,'decision':'accept','proposal':p['id'],'result':result}
            if not a.get('formal_id'):
                message = self.text('show','-s','--format=%B',result)
                if re.match(r'^\[[A-Z0-9]+-\d+\]', message):
                    self.formal_record(source,message,state='synced',result=result)
        if target != "master":
            ip = next((v for v in self.s["proposals"] if v["branch"] == target), None)
            if not ip:
                ip = {"id": uid("pi-"), "number": 19 + len(self.s["proposals"]) - 2, "title": "离线分发 · 组合候选", "author": plan["owner"], "branch": target, "base": self.oid("refs/heads/master"), "issue": p["issue"], "review": "pending", "history": [], "integrations": []}
                self.s["proposals"].append(ip)
            ip.update(candidate=result, label="I" + str(len(ip["history"]) + 1), review="pending")
            ip["history"].append({"label": ip["label"], "oid": result})
            self.git("update-ref", "refs/demo/candidates/" + uid("i-"), result)
            rec["integration_proposal"] = ip["id"]
        self.event(actor, "integration.executed", f"{p['label']} → {target} · {method} · {check['state']}", rec)
        return rec

    def cleanup_preview(self, actor, a):
        name = self.branch_name(a["branch"])
        branch = self.s["branches"].get(name)
        require(branch, "NOT_FOUND", "分支未登记", 404)
        require(actor == branch["owner"] or ACTORS[actor]["role"] == "maintainer", "FORBIDDEN", "仅负责人或维护者可管理此分支", 403)
        tip = self.oid("refs/heads/" + name)
        blockers = []
        if name == "master": blockers.append("受保护的正式线")
        if not tip: blockers.append("分支已删除")
        if branch["retention"]: blockers.append("保留原因：" + branch["retention"])
        mapping = [r for r in self.s["integrations"] if r["source"] == tip and r["state"] == "integrated"]
        if tip and not mapping: blockers.append("当前 tip 没有已执行的接纳映射（branch-only 不算）")
        if name.startswith("integration/") and any(i["state"] == "open" for i in self.s["issues"] if i["id"] == 42): blockers.append("Issue #42 仍在使用这条集成线")
        return {"branch": name, "expected_tip": tip, "allowed": not blockers, "blockers": blockers, "recovery": "先建立独立保留 ref，再条件删除；恢复时验证对象和名称", "observed_scope": "隔离示例仓库，无外部 clone / worktree"}

    def build(self, actor, a):
        self.maintainer(actor)
        oid = a["source"]
        require(re.fullmatch(r"[0-9a-f]{40}", oid or ""), "BAD_SOURCE", "构建源必须是固定提交", 422)
        require(oid == self.oid("refs/heads/master"), "SOURCE_CHANGED", "演示配方从当前 master 的固定快照构建；请重新选择")
        check = self.check(oid)
        self.s["checks"][oid] = check
        require(check["state"] == "passed", "CHECKS_FAILED", "构建检查未通过", 422, check)
        bid = uid("build-")
        folder = self.root / self.s["session"] / "artifacts" / bid
        folder.mkdir(parents=True)
        privacy, source_files = self.require_outbound(oid)
        manifest = {"schema": 1, "source": {"repository": "atlas-demo", "oid": oid}, "recipe": "portable-source-and-docs/v1", "builder": "Python stdlib " + sys.version.split()[0], "files": {n: digest(b.encode()) for n, b in source_files.items()}}
        artifacts = []
        for kind, names in [("source", source_files), ("docs", {n: b for n, b in source_files.items() if n.startswith("docs/") or n == "README.md"})]:
            path = folder / f"atlas-{kind}-{oid[:7]}.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
                for name, body in names.items(): z.writestr(name, body)
                z.writestr("BUILD-MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            raw = path.read_bytes()
            artifacts.append({"id": uid("pkg-"), "name": path.name, "kind": kind, "platform": "portable · source/docs", "digest": digest(raw), "size": len(raw), "file": str(path.relative_to(self.root)), "source": oid})
        build = {"id": bid, "source": oid, "recipe": manifest["recipe"], "time": now(), "state": "passed", "check": check, "artifacts": artifacts, "privacy": privacy}
        self.git("update-ref", "refs/demo/builds/" + bid, oid)
        self.s["builds"].append(build)
        self.event(actor, "build.completed", "已运行组合测试并生成两个真实 ZIP 包", {"build": bid, "source": oid})
        return build

    def find_artifact(self, aid):
        artifact = next((x for b in self.s["builds"] for x in b["artifacts"] if x["id"] == aid), None)
        require(artifact, "NOT_FOUND", "包不存在", 404)
        return artifact

    def artifact_path(self, aid):
        a = self.find_artifact(aid)
        path = (self.root / a["file"]).resolve()
        require(path.is_relative_to(self.root), "BAD_PATH", "产物路径错误", 422)
        return path

    def publish(self, actor, a):
        self.maintainer(actor)
        release = next((r for r in self.s["releases"] if r["id"] == a["release"]), None)
        require(release, "NOT_FOUND", "发布不存在", 404)
        if not a.get('reconcile'):
            for artifact in release['artifacts']:
                with zipfile.ZipFile(self.artifact_path(artifact['id'])) as archive:
                    files = {name:archive.read(name).decode() for name in archive.namelist() if not name.endswith('/')}
                self.require_outbound(release['source'], files, already_packaged=True)
        results = release["destinations"]
        if a.get("reconcile"):
            for destination, item in results.items():
                if item["state"] != "unknown": continue
                folder = self.root / self.s["session"] / "distribution" / destination / release["id"]
                valid = all((folder / pkg["name"]).exists() and digest((folder / pkg["name"]).read_bytes()) == pkg["digest"] for pkg in release["artifacts"])
                item.update(state="published" if valid else "failed", message="读取目标字节并核对摘要" if valid else "目标不完整，可重试", verified_at=now())
        else:
            for destination, item in results.items():
                if item["state"] in ["published", "unknown"]: continue
                item["attempts"] += 1
                fault = self.s["fault"] if destination == "team-mirror" else "none"
                if fault == "fail":
                    item.update(state="failed", message="演示注入：镜像暂不可用；发布资产已保留")
                    self.s["fault"] = "none"
                    continue
                folder = self.root / self.s["session"] / "distribution" / destination / release["id"]
                folder.mkdir(parents=True, exist_ok=True)
                for pkg in release["artifacts"]:
                    path = folder / pkg["name"]
                    shutil.copyfile(self.artifact_path(pkg["id"]), path)
                    require(digest(path.read_bytes()) == pkg["digest"], "DIGEST_MISMATCH", "目标校验不一致", 500)
                (folder / "release-manifest.json").write_text(json.dumps({k: v for k, v in release.items() if k != "destinations"}, ensure_ascii=False, indent=2), encoding="utf-8")
                item.update(state="unknown" if fault == "timeout" else "published", message="演示注入：字节已写入但回执丢失，先核对再重试" if fault == "timeout" else "真实文件复制完成 · SHA-256 一致", verified_at=now())
                if fault == "timeout": self.s["fault"] = "none"
        self.event(actor, "release.reconciled" if a.get("reconcile") else "release.distributed", f"{release['version']} · 各目标分别记录结果", results)
        return release

    def execute(self, actor, action, a):
        if action.startswith('workflow.'):
            return self.workflow_action(actor, action, a)
        if action.startswith(('publish.', 'format.', 'template.', 'privacy.', 'formal.', 'trial.', 'remote.')):
            return self.publish_action(actor, action, a)
        if action == "integration.preview": return self.integration_preview(actor, a)
        if action == "branch.preview": return self.cleanup_preview(actor, a)
        if action == "integration.execute": return self.integrate(actor, a)
        if action == "demo.reset":
            self.maintainer(actor)
            old_session, revision = self.s["session"], self.s["revision"]
            self.s = self.seed()
            self.ensure_publish()
            self.ensure_workflow()
            self.s["revision"] = revision
            self.event(actor, action, "已创建新示例；旧示例完整保留", {"retained_session": old_session})
            return {"session": self.s["session"], "retained": old_session}
        if action == "demo.advance":
            self.writer(actor)
            p = self.proposal("p17")
            require(actor in ["lin", "duo"], "FORBIDDEN", "由作者 Lin 或演示维护者推进夹具", 403)
            files = self.files(p["candidate"])
            if p["label"] == "A3":
                files["download.py"] = "def normalize_name(name):\n    cleaned = name.strip()\n    if not cleaned:\n        raise ValueError('package name is required')\n    return cleaned.replace(' ', '-')\n"
                label = "A4"
            else:
                label = "A" + str(int(p["label"][1:]) + 1)
                files["docs/usage.md"] += f"\nAuthor update {label}: clarify package handling.\n"
            new = self.make_commit(self.repo, files, [p["candidate"]], "fix: validate offline package names [" + label + "]", "Lin")
            self.git("update-ref", "refs/heads/" + p["branch"], new, p["candidate"])
            self.git("update-ref", "refs/demo/candidates/" + uid("a-"), new)
            p.update(candidate=new, label=label, review="pending")
            p["history"].append({"label": label, "oid": new})
            self.s["branches"][p["branch"]]["deleted"] = False
            self.event(actor, action, f"作者提交了 {label}；旧候选与旧行评论保持原锚点", {"source": new})
            return {"candidate": new, "label": label}
        if action == "checks.run":
            self.writer(actor)
            oid = a["source"]
            require(any(g["oid"] == oid for g in self.graph()), "BAD_SOURCE", "提交不在演示图中", 422)
            result = self.check(oid)
            self.s["checks"][oid] = result
            self.event(actor, action, "组合检查 · " + result["state"], result)
            return result
        if action.startswith("issue.") or action.startswith("comment."):
            self.collaboration()
            self.writer(actor)
            if action == "issue.create":
                title, body = str(a.get("title", "")).strip(), str(a.get("body", "")).strip()
                require(1 <= len(title) <= 160 and len(body) <= 10000, "INVALID_TEXT", "标题必填且不超过 160 字", 422)
                item = {"id": max(i["id"] for i in self.s["issues"]) + 1, "title": title, "body": body, "author": actor, "owner": actor, "state": "open", "version": 1, "comments": []}
                self.s["issues"].append(item)
                self.event(actor, action, f"创建 Issue #{item['id']}")
                return item
            issue = self.issue(a["issue"])
            if action == "issue.edit":
                require(actor == issue["author"] or ACTORS[actor]["role"] == "maintainer", "FORBIDDEN", "仅作者或维护者能编辑事项", 403)
                require(a["version"] == issue["version"], "EDIT_CONFLICT", "事项已被另一客户端编辑；保留草稿，请刷新后比较")
                require(a.get("title", "").strip(), "INVALID_TEXT", "标题不能为空", 422)
                require(a.get("state") in ["open", "closed"], "INVALID_STATE", "事项状态无效", 422)
                require(a.get("owner") in ACTORS and a["owner"] != "guest", "INVALID_OWNER", "负责人无效", 422)
                issue.update(title=str(a["title"])[:160], body=str(a.get("body", ""))[:10000], state=a["state"], owner=a["owner"], version=issue["version"] + 1)
                result = {k: v for k, v in issue.items() if k != "comments"}
            elif action == "comment.add":
                body = str(a.get("body", "")).strip()
                require(1 <= len(body) <= 10000, "INVALID_TEXT", "评论需要 1–10000 个字符", 422)
                candidate, anchor = a.get("candidate"), a.get("anchor")
                if candidate: require(any(g["oid"] == candidate for g in self.graph()), "STALE_CANDIDATE", "候选不存在", 422)
                if anchor:
                    require(candidate and anchor.get("path") in self.files(candidate), "BAD_ANCHOR", "文件不在固定候选中", 422)
                    count = len(self.files(candidate)[anchor["path"]].splitlines())
                    require(isinstance(anchor.get("line"), int) and 1 <= anchor["line"] <= count, "BAD_ANCHOR", "行号超出固定文件", 422)
                    anchor = {"path": anchor["path"], "line": anchor["line"], "side": "new", "head": candidate, "base": next((p["base"] for p in self.s["proposals"] if candidate in [h["oid"] for h in p["history"]]), None)}
                result = {"id": uid("comment-"), "author": actor, "body": body, "candidate": candidate, "anchor": anchor, "version": 1, "time": now()}
                issue["comments"].append(result)
            elif action == "comment.edit":
                comment = next((c for c in issue["comments"] if c["id"] == a["comment"]), None)
                require(comment, "NOT_FOUND", "评论不存在", 404)
                require(comment["author"] == actor, "FORBIDDEN", "只能编辑自己的评论", 403)
                require(a["version"] == comment["version"], "EDIT_CONFLICT", "评论已有新版本；草稿已保留")
                require(str(a.get("body", "")).strip(), "INVALID_TEXT", "评论不能为空", 422)
                comment.update(body=str(a["body"])[:10000], version=comment["version"] + 1, edited_at=now())
                result = comment
            else: raise Problem("UNKNOWN_COMMAND", "未知讨论命令", 404)
            self.event(actor, action, f"Issue #{issue['id']} · 讨论已保存")
            return result
        if action == "review.record":
            self.collaboration()
            self.maintainer(actor)
            p = self.proposal(a["proposal"])
            require(a["source"] == p["candidate"], "STALE_CANDIDATE", "候选已更新")
            require(a["verdict"] in ["approved", "needs-work"], "BAD_REVIEW", "审查决定无效", 422)
            p.update(review=a["verdict"], reviewed_oid=p["candidate"])
            self.event(actor, action, f"{p['label']} · {a['verdict']}；审查不代表已合入")
            return {"source": p["candidate"], "review": p["review"]}
        if action == "branch.keep":
            info = self.cleanup_preview(actor, a)
            self.s["branches"][info["branch"]]["retention"] = str(a.get("reason", ""))[:500]
            self.event(actor, action, "已更新分支保留原因", info)
            return info
        if action == "branch.delete":
            info = self.cleanup_preview(actor, a)
            require(info["expected_tip"] == a.get("expected_tip"), "BRANCH_MOVED", "分支已有新提交，不能按旧快照删除")
            require(info["allowed"], "BRANCH_IN_USE", "分支仍需保留", 409, info)
            archive = "refs/demo/archive/" + uid("keep-")
            self.git("update-ref", archive, info["expected_tip"])
            self.git("update-ref", "-d", "refs/heads/" + info["branch"], info["expected_tip"])
            self.s["branches"][info["branch"]].update(deleted=True, recovery_ref=archive, archived_oid=info["expected_tip"])
            self.event(actor, action, "分支已删除；Git 对象由恢复引用保留", {**info, "recovery_ref": archive})
            return {**info, "recovery_ref": archive}
        if action == "branch.restore":
            name = self.branch_name(a["branch"])
            b = self.s["branches"].get(name)
            require(b and b.get("deleted"), "NOT_FOUND", "没有可恢复记录", 404)
            require(actor == b["owner"] or ACTORS[actor]["role"] == "maintainer", "FORBIDDEN", "无恢复权限", 403)
            require(not self.oid("refs/heads/" + name), "BRANCH_EXISTS", "同名分支已存在")
            oid = self.oid(b["recovery_ref"])
            require(oid, "MISSING_OBJECT", "保留对象不可用")
            self.git("update-ref", "refs/heads/" + name, oid, "0" * 40)
            b["deleted"] = False
            self.event(actor, action, "已从保留引用恢复分支", {"branch": name, "oid": oid})
            return {"branch": name, "oid": oid}
        if action == "build.create": return self.build(actor, a)
        if action == "artifact.verify":
            pkg = self.find_artifact(a["artifact"])
            actual = digest(self.artifact_path(pkg["id"]).read_bytes())
            require(actual == pkg["digest"], "DIGEST_MISMATCH", "实际字节与清单不一致", 422)
            self.event(actor, action, "已读取实际文件并验证 SHA-256", {"artifact": pkg["id"], "digest": actual})
            return {"verified": True, "digest": actual, "bytes": pkg["size"]}
        if action == "release.create":
            self.maintainer(actor)
            build = next((b for b in self.s["builds"] if b["id"] == a["build"]), None)
            require(build, "NOT_FOUND", "构建不存在", 404)
            version = str(a.get("version", ""))
            require(re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?", version), "BAD_VERSION", "请输入版本，例如 0.8.0-preview.1", 422)
            previous = [r for r in self.s["releases"] if r["version"] == version]
            require(not previous or ("-" in version and a.get("replace_preview") and all(not r.get("immutable") for r in previous)), "IMMUTABLE_RELEASE", "同名稳定版本不可替换；预发布更新需显式勾选新修订")
            rid = uid("release-")
            result = {"id": rid, "version": version, "revision": len(previous) + 1, "source": build["source"], "build": build["id"], "artifacts": copy.deepcopy(build["artifacts"]), "notes": str(a.get("notes", ""))[:10000], "time": now(), "immutable": "-" not in version, "destinations": {k: {"state": "pending", "attempts": 0, "message": "待发布"} for k in ["release-assets", "team-mirror"]}}
            tag = "refs/tags/v" + version.lstrip("v")
            current = self.oid(tag)
            if current and not previous: raise Problem("TAG_EXISTS", "同名标签已被其他来源占用")
            self.git("update-ref", tag, result["source"], current or "0" * 40)
            self.git("update-ref", "refs/demo/releases/" + rid, result["source"])
            self.s["releases"].append(result)
            self.event(actor, action, f"准备 {version} / r{result['revision']} · 固定来源、标签与包清单")
            return result
        if action in ["release.publish", "release.reconcile"]: return self.publish(actor, {**a, "reconcile": action == "release.reconcile"})
        if action == "channel.set":
            self.maintainer(actor)
            release = next((r for r in self.s["releases"] if r["id"] == a["release"]), None)
            require(release, "NOT_FOUND", "发布不存在", 404)
            require(a["channel"] in ["preview", "stable"], "BAD_CHANNEL", "渠道无效", 422)
            require(all(d["state"] == "published" for d in release["destinations"].values()), "DISTRIBUTION_INCOMPLETE", "所有分发目标确认成功后才能更新渠道")
            require(a["channel"] != "stable" or "-" not in release["version"], "PRERELEASE", "stable 需要一个稳定版本号")
            self.s["channels"][a["channel"]] = release["id"]
            self.event(actor, action, f"{a['channel']} → {release['version']} / r{release['revision']}", {"reason": str(a.get("reason", "用户选择此修订"))[:500]})
            return {"channel": a["channel"], "release": release["id"]}
        if action == "settings.save":
            self.maintainer(actor)
            provider = a.get("provider", self.s["provider"])
            require(provider in PROVIDERS, "UNSUPPORTED", "未知适配器", 422)
            self.s["provider"] = provider
            for field in ["api", "web", "git", "registry", "prefix"]:
                if field in a: self.s["config"][field] = str(a[field])[:500]
            if "privacy" in a: self.s["config"]["privacy"] = bool(a["privacy"])
            self.event(actor, action, "演示连接配置已保存；外部地址只展示，不发起网络请求")
            return {"provider": provider, "capabilities": PROVIDERS[provider], "config": self.s["config"]}
        if action == "demo.fault":
            self.maintainer(actor)
            require(a["fault"] in ["none", "fail", "timeout"], "BAD_FAULT", "故障类型无效", 422)
            self.s["fault"] = a["fault"]
            self.event(actor, action, "下一次团队镜像分发：" + a["fault"])
            return {"fault": a["fault"]}
        raise Problem("UNKNOWN_COMMAND", "未知命令", 404)

    def command(self, actor, payload):
        require(actor in ACTORS, "UNKNOWN_ACTOR", "演示账户不存在", 403)
        request_id = str(payload.get("request_id", ""))
        require(re.fullmatch(r"[A-Za-z0-9_-]{8,100}", request_id), "REQUEST_ID_REQUIRED", "需要有效 request_id", 422)
        fingerprint = digest(encode({"actor": actor, "action": payload.get("action"), "args": payload.get("args", {})}).encode())
        with self.lock:
            previous = self.db.execute("SELECT fingerprint, body FROM receipts WHERE id=?", (request_id,)).fetchone()
            if previous:
                require(previous[0] == fingerprint, "IDEMPOTENCY_MISMATCH", "请求 ID 已用于另一项操作")
                return {**json.loads(previous[1]), "replayed": True}
            require(payload.get("expected_revision") == self.s["revision"], "STALE_STATE", "另一窗口已更新状态。当前草稿保留；刷新后重新核对再提交。", 409, {"current_revision": self.s["revision"]})
            before = copy.deepcopy(self.s)
            action, args = str(payload.get("action")), payload.get("args", {})
            try:
                result = self.execute(actor, action, args)
                readonly = action.endswith(".preview")
                if not readonly: self.s["revision"] += 1
                response = {"ok": True, "request_id": request_id, "action": action, "revision": self.s["revision"], "result": result, "time": now(), "authority": "local-demo-server"}
                self.db.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)", (request_id, actor, fingerprint, encode(response), now()))
                self.save()
                return response
            except Exception:
                self.s = before
                self.db.rollback()
                raise

class Handler(BaseHTTPRequestHandler):
    server_version = "GitgoPublishDemo/1"
    def log_message(self, fmt, *args): pass
    @property
    def demo(self): return self.server.demo
    def actor(self): return self.headers.get("X-Demo-Actor", "duo")
    def reply(self, code, data, mime="application/json; charset=utf-8", attachment=None):
        raw = encode(data).encode() if not isinstance(data, bytes) else data
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment: self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        self.wfile.write(raw)
    def error(self, e): self.reply(e.status, {"ok": False, "error": {"code": e.code, "message": e.message, "details": e.details}})
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            actor = self.actor()
            require(actor in ACTORS, "UNKNOWN_ACTOR", "账户不存在", 403)
            if parsed.path == "/api/state": return self.reply(200, self.demo.snapshot(actor))
            if parsed.path == "/api/diff":
                q = parse_qs(parsed.query)
                oid = q.get("oid", [""])[0]
                require(re.fullmatch(r"[0-9a-f]{40}", oid), "BAD_SOURCE", "提交无效", 422)
                with self.demo.lock:
                    p = next((p for p in self.demo.s["proposals"] if oid in [h["oid"] for h in p["history"]]), None)
                    base = p["base"] if p else self.demo.text("rev-list", "--max-parents=0", oid).splitlines()[0]
                    diff = self.demo.text("diff", "--no-ext-diff", "--no-textconv", "--unified=4", base, oid, "--")
                    return self.reply(200, {"source": oid, "base": base, "text": diff, "files": self.demo.files(oid)})
            if parsed.path.startswith("/api/artifacts/"):
                aid = parsed.path.rsplit("/", 1)[1]
                with self.demo.lock:
                    path = self.demo.artifact_path(aid)
                    return self.reply(200, path.read_bytes(), "application/zip", path.name)
            if parsed.path == "/api/export":
                with self.demo.lock:
                    content = json.dumps({"state": self.demo.snapshot(actor), "receipts": [json.loads(row[0]) for row in self.demo.db.execute("SELECT body FROM receipts ORDER BY created")]}, ensure_ascii=False, indent=2).encode()
                    return self.reply(200, content, "application/json", "publish-demo-evidence.json")
            name = {"/": "index.html", "/index.html": "index.html", "/terminal.js": "terminal.js", "/publish-ui.js": "publish-ui.js", "/commit-layout.js": "commit-layout.js", "/graphs.js": "graphs.js", "/terminal.css": "terminal.css"}.get(parsed.path)
            require(name, "NOT_FOUND", "页面不存在", 404)
            mime = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}[name.rsplit(".", 1)[1]]
            return self.reply(200, (ROOT / name).read_bytes(), mime)
        except Problem as e: self.error(e)
        except Exception as e: self.reply(500, {"ok": False, "error": {"code": "SERVER_ERROR", "message": str(e)}})
    def do_POST(self):
        try:
            require(self.path == "/api/command", "NOT_FOUND", "命令入口不存在", 404)
            # No CORS. A cross-site form cannot produce application/json + this header.
            require(self.headers.get("X-Demo-Client") == "publish-demo", "BAD_CLIENT", "需要同源演示客户端", 403)
            origin = self.headers.get("Origin")
            require(not origin or origin == "http://" + self.headers.get("Host", ""), "BAD_ORIGIN", "仅允许同源请求", 403)
            require(self.headers.get("Content-Type", "").startswith("application/json"), "BAD_TYPE", "需要 JSON 请求", 415)
            length = int(self.headers.get("Content-Length", "0"))
            require(0 < length <= 50000, "BAD_SIZE", "请求过大或为空", 413)
            payload = json.loads(self.rfile.read(length))
            result = self.demo.command(self.actor(), payload)
            self.reply(200, result)
        except Problem as e: self.error(e)
        except (ValueError, KeyError, TypeError) as e: self.reply(422, {"ok": False, "error": {"code": "INVALID_REQUEST", "message": str(e)}})
        except Exception as e: self.reply(500, {"ok": False, "error": {"code": "SERVER_ERROR", "message": str(e)}})

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=ROOT / "runtime")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    demo = Demo(args.data)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.demo = demo
    print(f"Gitgo /publish executable demo: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Isolated data: {args.data.resolve()}", flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{server.server_port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); demo.db.close()

if __name__ == "__main__": main()
