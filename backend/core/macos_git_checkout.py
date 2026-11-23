"""A bounded commit image, produced inside the existing Git execution lease.

This prepares working files; it does not register a worktree or publish files
into a project. The ordinary Host verifies the commit and recomputes the entire
tree from the returned bytes. No task-authored index or path becomes authority.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct
import unicodedata

from backend.core.loop.git_execution import GitBoundaryError
from backend.core import macos_broker_protocol as wire

MAX_COMMIT = 65536
MAX_OUTPUT = 100000


def refuse(message):
    raise GitBoundaryError('GIT_CHECKOUT_UNSUPPORTED', message)


def validate_commit(commit):
    if type(commit) is not str or re.fullmatch('[0-9a-f]{40}', commit) is None:
        refuse('Checkout requires one exact SHA-1 commit, without revision expressions')


def object_oid(kind, data):
    return hashlib.sha1(kind.encode('ascii') + b' ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


# Trusted code, supplied by the broker rather than the request. It runs under
# the SAME low-UID/Seatbelt lease as Git and its descendants. cat-file without
# --filters yields exact blob bytes: attributes cannot run filters or silently
# transform newlines/working-tree encodings. Pipes and aggregate output are
# bounded; the supervisor enforces the overall time/resource budget.
PRODUCER = r'''
import base64,json,os,re,subprocess,sys
destination,checkout,commit=sys.argv[1:]
if re.fullmatch('[0-9a-f]{40}',commit) is None: raise ValueError('Exact commit required')
prefix=['/usr/bin/git','--no-pager','--no-lazy-fetch','--no-replace-objects',
 '--git-dir='+destination,'-c','safe.directory='+destination,
 '-c','core.hooksPath=/dev/null','-c','core.fsmonitor=false',
 '-c','protocol.allow=never','-c','gc.auto=0','-c','maintenance.auto=false']
def read(args,limit):
 p=subprocess.Popen(prefix+args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
 try:
  data=p.stdout.read(limit+1)
  if len(data)>limit: raise ValueError('Git output exceeds budget')
  if p.wait()!=0: raise ValueError('Git object read failed')
  return data
 finally:
  p.stdout.close()
  if p.poll() is None: p.kill();p.wait()
raw=read(['cat-file','commit',commit],65536)
listing=read(['ls-tree','-r','-z','--full-tree',commit],150000)
if listing and not listing.endswith(b'\0'): raise ValueError('Truncated tree')
entries=[]
for line in listing.split(b'\0')[:-1]:
 header,name=line.split(b'\t',1);mode,kind,oid=header.split(b' ')
 if mode not in (b'100644',b'100755') or kind!=b'blob' or re.fullmatch(b'[0-9a-f]{40}',oid) is None:
  raise ValueError('Only regular files supported')
 name=name.decode('utf-8',errors='strict');parts=name.split('/')
 if len(name.encode())>1024 or len(parts)>64 or any(not p or p in ('.','..') or
    p.casefold() in ('.git','.gitgo') or '\\' in p or ':' in p for p in parts):
  raise ValueError('Unsafe checkout path')
 entries.append((name,mode,oid.decode('ascii')))
 if len(entries)>128: raise ValueError('Too many checkout files')
used=0
for name,mode,oid in entries:
 data=read(['cat-file','blob',oid],4194304-used);used+=len(data)
 path=os.path.join(checkout,name);os.makedirs(os.path.dirname(path),mode=0o700,exist_ok=True)
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as stream: stream.write(data)
 os.chmod(path,0o755 if mode==b'100755' else 0o644)
print(json.dumps({'commit':base64.b64encode(raw).decode('ascii')}))
'''


def command(destination, checkout, commit):
    validate_commit(commit)
    destination, checkout = Path(destination), Path(checkout)
    if (not destination.is_absolute() or '..' in destination.parts or
            destination.name != 'object-view' or checkout != destination.parent / 'checkout'):
        refuse('Expected broker-owned object and checkout directories')
    from backend.core.child_process import python_command
    return python_command(['-I', '-c', PRODUCER, str(destination), str(checkout), commit])


def validate_paths(files, directories):
    """Conservative portable subset; reject aliases before Host publication."""
    required = set()
    aliases = {}
    for name in [n for n, _, _ in files] + list(directories):
        parts = name.split('/')
        if len(parts) > 64:
            refuse('Checkout path exceeds depth budget')
        for i, part in enumerate(parts):
            if (part.endswith((' ', '.')) or ':' in part or
                    any(unicodedata.category(c).startswith('C') for c in part) or
                    unicodedata.normalize('NFD', part).casefold() in ('.git', '.gitgo')):
                refuse('Checkout path is a metadata or filesystem alias')
            prefix = '/'.join(parts[:i + 1])
            alias = unicodedata.normalize('NFD', prefix).casefold()
            if aliases.setdefault(alias, prefix) != prefix:
                refuse('Checkout paths alias on macOS')
        if name in {n for n, _, _ in files}:
            required.update('/'.join(parts[:i]) for i in range(1, len(parts)))
    if set(directories) != required:
        refuse('Checkout has missing or unexpected directories')


def tree_oid(files):
    tree = {}
    for name, data, mode in files:
        node = tree
        parts = name.split('/')
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                refuse('Checkout file/directory conflict')
        if parts[-1] in node:
            refuse('Duplicate checkout entry')
        node[parts[-1]] = (mode, object_oid('blob', data))

    def digest(node):
        entries = []
        for name, value in node.items():
            directory = isinstance(value, dict)
            mode, oid = ('40000', digest(value)) if directory else (
                '100755' if value[0] == 0o755 else '100644', value[1])
            encoded = name.encode('utf8')
            entries.append((encoded + (b'/' if directory else b'\0'),
                            mode.encode() + b' ' + encoded + b'\0' + bytes.fromhex(oid)))
        return object_oid('tree', b''.join(data for _, data in sorted(entries)))
    return digest(tree)


def canonical_index(files):
    """Host-generated v2 index, no untrusted stat cache or index extensions."""
    data = bytearray(struct.pack('!4sII', b'DIRC', 2, len(files)))
    for name, raw, mode in sorted(files, key=lambda item: item[0].encode('utf8')):
        path = name.encode('utf8')
        entry = (struct.pack('!10I', 0, 0, 0, 0, 0, 0, 0o100755 if mode == 0o755 else 0o100644,
                             0, 0, 0) + bytes.fromhex(object_oid('blob', raw)) +
                 struct.pack('!H', len(path)) + path + b'\0')
        data.extend(entry + b'\0' * (-len(entry) % 8))
    return bytes(data) + hashlib.sha1(data).digest()


@dataclass(frozen=True)
class CheckoutImage:
    commit: str
    files: tuple[tuple[str, bytes, int], ...]
    directories: tuple[str, ...]
    index: bytes


def verify_result(commit, image, output):
    validate_commit(commit)
    if type(output) is not str or len(output.encode('utf8')) > MAX_OUTPUT:
        refuse('Missing or excessive checkout proof')
    try:
        proof = json.loads(output, object_pairs_hook=wire.unique_pairs)
        if type(proof) is not dict or set(proof) != {'commit'} or type(proof['commit']) is not str:
            raise ValueError('Invalid proof')
        raw = base64.b64decode(proof['commit'], validate=True)
    except (ValueError, TypeError) as exc:
        refuse('Invalid checkout proof: ' + str(exc))
    if len(raw) > MAX_COMMIT or object_oid('commit', raw) != commit:
        refuse('Checkout commit content does not match the requested object')
    header = raw.split(b'\n\n', 1)[0].split(b'\n')
    if (not header or re.fullmatch(b'tree [0-9a-f]{40}', header[0]) is None or
            sum(line.startswith(b'tree ') for line in header) != 1):
        refuse('Invalid commit tree header')
    files, directories = wire.validate_image(image)
    if any(mode not in (0o644, 0o755) for _, _, mode in files):
        refuse('Checkout accepts only canonical ordinary file modes')
    validate_paths(files, directories)
    if tree_oid(files) != header[0][5:].decode('ascii'):
        refuse('Checkout files or modes differ from the requested commit tree')
    files = tuple(sorted(files))
    return CheckoutImage(commit, files, tuple(sorted(directories)), canonical_index(files))
