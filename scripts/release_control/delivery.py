"""Reconcile external effects before writing; immutable releases, resumable feeds."""
import hashlib
import time
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from .assets import inputs, validate_fresh
from .evidence import file_fact
from .plan import check_source


def matches(row, fact):
    return (row.get('state') == 'uploaded' and row.get('size') == fact['bytes']
            and row.get('digest') == 'sha256:' + fact['sha256'])


def guard(plan, prepared):
    check_source(plan)
    validate_fresh(prepared)
    if inputs(plan) != prepared['inputs']:
        raise ValueError('Prepared archive/baseline inputs changed')
    if any(file_fact(p) != f for p, f in prepared['assets'].items()):
        raise ValueError('Prepared release bytes changed')


def ci_gate(plan, evidence, remote):
    evidence.record('ci', 'running')
    try:
        remote.source(plan['release'])
        facts = [remote.ci(item, plan['release']) for item in plan['ci']]
    except Exception as exc:
        evidence.record('ci', 'failed', error=type(exc).__name__)
        raise
    return evidence.record('ci', 'success', runs=facts)


def write(evidence, name, action, **intent):
    evidence.record('remote-' + name, 'intent', **intent)
    # Never automatically replay a mutation whose response was lost. The next
    # invocation first re-observes the resource and only fills missing effects.
    action()
    evidence.record('remote-' + name, 'returned', **intent)


def draft(plan, evidence, remote, prepared):
    guard(plan, prepared)
    identity = plan['release']
    tag, commit = identity['tag'], identity['commit']
    observed = remote.tag(tag)
    if observed not in (None, commit):
        raise ValueError('Release tag points at a different source')
    body = Path(plan['notes']).read_text(encoding='utf-8')
    body += '\n\n<!-- gitgo-release-plan:' + evidence.identity + ' -->\n'
    row = remote.release(tag)
    if row and (row['body'] != body or row['prerelease'] != (identity['channel'] == 'preview')):
        raise ValueError('Existing release belongs to a different plan')
    if observed is None:
        write(evidence, 'tag', lambda: remote.api('git/refs', 'POST', {'ref': 'refs/tags/' + tag, 'sha': commit}))
    if remote.tag(tag) != commit:
        raise ValueError('Tag creation could not be verified')
    if row is None:
        write(evidence, 'draft', lambda: remote.api('releases', 'POST', {
            'tag_name': tag, 'target_commitish': commit, 'name': plan.get('title', tag),
            'body': body, 'draft': True, 'prerelease': identity['channel'] == 'preview', 'make_latest': 'false'}))
        row = remote.release(tag)
    if row is None or row['body'] != body:
        raise ValueError('Draft creation could not be verified')
    expected = {Path(p).name: (Path(p), f) for p, f in prepared['assets'].items()}
    found = remote.assets(row)
    if set(found) - expected.keys():
        raise ValueError('Release has unexpected assets; refusing to delete them')
    for name, (path, fact) in expected.items():
        if name not in found:
            if not row['draft']:
                raise ValueError('Published release is incomplete; refusing to alter immutable release')
            write(evidence, 'upload-' + name, lambda path=path: remote.upload(tag, path), expected=fact)
            found = remote.assets(row)
        if name not in found or not matches(found[name], fact):
            raise ValueError('Remote release asset differs: ' + name)
    evidence.record('remote-draft', 'verified', release_id=row['id'], assets=len(found))
    return row


def advance_feeds(plan, evidence, remote, prepared):
    tag = 'updates-' + plan['release']['channel']
    feed = remote.release(tag)
    if not feed or feed['draft']:
        raise ValueError('Existing public channel feed is required')
    wanted = {}
    for name, target in plan['targets'].items():
        for suffix, baseline_key in (('.manifest.sig', 'baseline_signature'), ('.manifest.json', 'baseline_manifest')):
            path = evidence.root/'assets'/(name + suffix)
            wanted[path.name] = (path, prepared['assets'][str(path)], file_fact(target[baseline_key]))
    # Audit the entire feed before any removal. Both old and new are admissible
    # so a partial prior update can resume without rolling back another target.
    found = remote.assets(feed)
    journal = evidence.read('feed-baseline')
    if journal and journal['release_id'] != feed['id']:
        raise ValueError('Feed release identity changed')
    if journal is None:
        journal = evidence.record('feed-baseline', 'observed', release_id=feed['id'],
            assets={n: r['id'] for n, r in found.items() if n in wanted})
    for name, (_, new, old) in wanted.items():
        row = found.get(name)
        if row is None:
            if not evidence.read('remote-feed-delete-' + name):
                raise ValueError('Feed asset absent without a recorded deletion intent: ' + name)
        elif not matches(row, new) and not (matches(row, old) and row['id'] == journal['assets'].get(name)):
            raise ValueError('Channel feed changed outside this release: ' + name)
    # Signature first then manifest. GitHub does not support an atomic pair;
    # clients fail closed during the short mismatch window. Never claim atomicity.
    for name, (path, new, old) in wanted.items():
        found = remote.assets(feed)
        row = found.get(name)
        if row and matches(row, new):
            continue
        if row:
            if not matches(row, old) or row['id'] != journal['assets'].get(name):
                raise ValueError('Feed changed before replacement: ' + name)
            write(evidence, 'feed-delete-' + name,
                lambda row=row: remote.api('releases/assets/' + str(row['id']), 'DELETE'), asset_id=row['id'])
        write(evidence, 'feed-upload-' + name, lambda path=path: remote.upload(tag, path), expected=new)
        current = remote.assets(feed).get(name)
        if not current or not matches(current, new):
            raise ValueError('Channel feed replacement not verified: ' + name)
    evidence.record('feeds', 'success', release_id=feed['id'], assets=len(wanted))


def publish(plan, evidence, remote, prepared, row):
    guard(plan, prepared)
    ci_gate(plan, evidence, remote)  # A rerun/cancellation invalidates old green.
    if remote.tag(plan['release']['tag']) != plan['release']['commit']:
        raise ValueError('Release tag moved before publication')
    if row['draft']:
        write(evidence, 'publish', lambda: remote.api('releases/' + str(row['id']), 'PATCH',
            {'draft': False, 'make_latest': 'false'}))
    row = remote.release(plan['release']['tag'])
    if not row or row['draft']:
        raise ValueError('Publication not observed')
    advance_feeds(plan, evidence, remote, prepared)
    evidence.record('published', 'success', release_id=row['id'])


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'github.com', 'release-assets.githubusercontent.com',
            'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Public asset URL is outside release hosts')


def download_fact(url, expected):
    validate_url(url)
    hasher, size = hashlib.sha256(), 0
    deadline = time.monotonic() + 900
    # Anonymous request, streamed and bounded by the actual signed asset size.
    with build_opener(PublicRedirects()).open(url, timeout=90) as response:
        while chunk := response.read(1024 * 1024):
            if time.monotonic() > deadline:
                raise ValueError('Public asset download exceeded its deadline')
            size += len(chunk)
            if size > expected['bytes']:
                raise ValueError('Public asset exceeds signed size')
            hasher.update(chunk)
    return {'bytes': size, 'sha256': hasher.hexdigest()}


def verify_public(plan, evidence, prepared, fetch=download_fact):
    release = plan['release']
    base = 'https://github.com/' + release['repository'] + '/releases/download/'
    evidence.record('public-downloads', 'running')
    verified = []
    for path, fact in prepared['assets'].items():
        name = Path(path).name
        for tag in [release['tag']] + (['updates-' + release['channel']] if '.manifest.' in name else []):
            url = base + quote(tag, safe='') + '/' + quote(name, safe='')
            if fetch(url, fact) != fact:
                raise ValueError('Anonymous public download mismatch: ' + name)
            verified.append({'tag': tag, 'name': name, **fact})
    return evidence.record('public-downloads', 'success', assets=verified,
        native_managed_install_verified=False, formal_terminal_verified=False)
