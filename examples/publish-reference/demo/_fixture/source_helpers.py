"""Isolated reference fixture. No real accounts, remote forge or product state."""
import ast
import hashlib
import ipaddress
import re
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent / 'source-snapshot'
SOURCES = {}

class DropBackendImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        if (node.module or '').startswith('backend') or node.level:
            return None
        return node

def load(relative, names=None, context=None):
    path = REPO / relative
    raw = path.read_text(encoding='utf-8')
    SOURCES[relative] = hashlib.sha256(raw.encode()).hexdigest()
    tree = ast.parse(raw, filename=str(path))
    if names is not None:
        tree.body = [n for n in tree.body if
            isinstance(n, (ast.Import, ast.ImportFrom)) or
            isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names or
            isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets) or
            isinstance(n, ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id in names]
    tree = DropBackendImports().visit(tree)
    ast.fix_missing_locations(tree)
    scope = {'__name__':'publish_demo_source', 'Path':Path, 're':re,
             'hashlib':hashlib, 'ipaddress':ipaddress, **(context or {})}
    exec(compile(tree, str(path), 'exec'), scope)
    return scope

CFG = load('backend/core/config.py', {
    'DEFAULT_FORCE_EXCLUDE','DEFAULT_COMMIT_FORMAT','DEFAULT_SECURITY_SCAN',
    'default_outbound_policy','normalize_outbound_policy'})
UTIL = load('backend/core/operations/utils.py', {'_match_glob'})
SECURITY = load('backend/core/operations/security.py', {
    'DEFAULT_SECURITY_PATTERNS','_SOURCE_CODE_SUFFIXES','_is_source_reference','scan_diff_for_secrets'})
PRIVACY = load('backend/core/authorship.py', context={
    '_match_glob':UTIL['_match_glob'], 'scan_diff_for_secrets':SECURITY['scan_diff_for_secrets']})
FORMAT = load('backend/core/template_manager.py', {'_DEFAULT_HEADER','_DEFAULT_BODY'})

def default_policy(): return CFG['default_outbound_policy']()
def normalize_policy(value): return CFG['normalize_outbound_policy'](value)
def match(pattern, path): return UTIL['_match_glob'](pattern,path)
def private(path, content):
    return PRIVACY['is_ai_config_file'](path) or PRIVACY['looks_like_private_tool_content'](content)
def scan(path, content, policy):
    diff = f'+++ b/{path}\n@@ -0,0 +1,{len(content.splitlines())} @@\n'+'\n'.join('+'+line for line in content.splitlines())
    # Core high/critical credential checks remain active even when optional scanning is off.
    secrets = SECURITY['scan_diff_for_secrets'](diff, config=policy)
    approved = set(policy['approved_fingerprints'])
    findings = [{**x,'level':'error','rule':x['rule_id']} for x in secrets
                if x.get('match_fingerprint') not in approved and
                (policy['enabled'] or x['severity'] in ('high','critical'))]
    if policy['enabled']:
        findings += [x for x in PRIVACY['scan_privacy'](path, content,
            level=policy['content_level'], deep_scan=policy['deep_scan'])
            if not any(e.get('sha256') in approved for e in x.get('evidence',[]))]
    return findings

def format_message(demo, source, config, templates):
    class Runner:
        def is_git_repo(self): return True
        def log(self, **kw): return demo.text('log','refs/heads/master','--format=%s','-1000').splitlines()
    manager = SimpleNamespace(get_template=lambda name:SimpleNamespace(**templates[name]) if name in templates else None)
    builtin = SimpleNamespace(header_format=FORMAT['_DEFAULT_HEADER'], body_format=FORMAT['_DEFAULT_BODY'],prefix_override=None)
    scope = load('backend/core/operations/git.py', {'_find_next_number','build_commit_template','validate_commit_message'},
                 {'TemplateManager':manager,'_BUILTIN_DEFAULT':builtin})
    p = next((p for p in demo.s['proposals'] if p['candidate']==source),None)
    base = p['base'] if p else demo.oid('refs/heads/master')
    logs = demo.text('log',f'{base}..{source}','--format=%H%x09%s').splitlines()
    if not logs: logs=[source+'\t'+demo.text('show','-s','--format=%s',source)]
    commits=[]
    for line in logs:
        oid, subject=line.split('\t',1)
        m=re.match(r'(\w+)(?:\(([^)]+)\))?:\s*(.*)',subject)
        commits.append(SimpleNamespace(hash=oid,type=m[1] if m else 'chore',scope=m[2] if m else '',subject=m[3] if m else subject))
    # Reserve numbers already held by local formal drafts as well as Git history.
    config=dict(config)
    used=[f['number'] for f in demo.s['formal_commits'] if f['prefix']==config['prefix']]
    config['number_start']=max([int(config['number_start'])]+[n+1 for n in used])
    project=SimpleNamespace(name='atlas',commit_format=config,backup_path=str(demo.repo))
    message=scope['build_commit_template'](commits,project,git_runner=Runner())
    return '\n'.join(line for line in message.splitlines() if not line.startswith('# 请编辑')).rstrip()
