"""Build-only relocation of an explicitly selected local Node/npm toolchain.

Never run by the broker. Resolves only Mach-O dependency closure and npm's own
package; no blanket Homebrew grant. Output enters sealed helper inventory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def run(*args):return subprocess.check_output(args,text=True,stderr=subprocess.PIPE,timeout=30)

def system(path):return path.startswith(('/usr/lib/','/System/'))


def dependencies(path):
    return [line.strip().split(' (compatibility')[0] for line in run('/usr/bin/otool','-L',str(path)).splitlines()[1:]]


def resolve(reference,image,entry):
    if reference.startswith('@loader_path/'):
        candidates=[image.parent/reference[len('@loader_path/'):]]
    elif reference.startswith('@executable_path/'):
        candidates=[entry.parent/reference[len('@executable_path/'):]]
    elif reference.startswith('@rpath/'):
        suffix=reference[len('@rpath/'):]
        lines=run('/usr/bin/otool','-l',str(image))
        paths=re.findall(r'cmd LC_RPATH\n.*?\n\s*path (.*?) \(offset',lines,re.S)
        candidates=[]
        for root in paths:
            root=root.replace('@loader_path',str(image.parent)).replace('@executable_path',str(entry.parent))
            if root.startswith('/'):candidates.append(Path(root)/suffix)
        candidates += [image.parent/suffix,image.parent.parent/'lib'/suffix]
    elif reference.startswith('/'):candidates=[Path(reference)]
    else:raise ValueError('Unsupported dependency '+reference)
    for path in candidates:
        if path.is_file():return path.resolve(strict=True)
    raise ValueError('Unresolved dependency '+reference+' of '+str(image))


def stage(node,destination):
    if os.geteuid()==0:raise ValueError('Build toolchain as ordinary user, then seal with installer')
    entry=Path(node).resolve(strict=True);dest=Path(destination)
    if dest.exists():raise ValueError('Use a fresh toolchain destination')
    npm=entry.parent.parent/'lib/node_modules/npm'
    if not (npm/'bin/npm-cli.js').is_file():raise ValueError('Matching npm package missing')
    edges={};todo=[entry];total=0
    while todo:
        path=todo.pop()
        if path in edges:continue
        if len(edges)>=128:raise ValueError('Dependency closure too large')
        total+=path.stat().st_size
        if total>512*1024**2:raise ValueError('Toolchain exceeds build budget')
        refs={}
        for ref in dependencies(path):
            if system(ref):continue
            target=resolve(ref,path,entry)
            if target==path:continue  # dylib ID, not a load dependency
            refs[ref]=target;todo.append(target)
        edges[path]=refs
    dest.mkdir(parents=True);(dest/'bin').mkdir();(dest/'lib').mkdir()
    targets={p:dest/('bin/node' if p==entry else 'lib/'+hashlib.sha256(str(p).encode()).hexdigest()[:12]+'-'+p.name) for p in edges}
    evidence=[]
    for original,target in targets.items():
        shutil.copyfile(original,target);target.chmod(0o755)
        evidence.append(dict(source=str(original),sha256=hashlib.sha256(original.read_bytes()).hexdigest(),staged=str(target.relative_to(dest))))
    for original,refs in edges.items():
        target=targets[original]
        for ref,dep in refs.items():
            replacement='@loader_path/'+os.path.relpath(targets[dep],target.parent)
            run('/usr/bin/install_name_tool','-change',ref,replacement,str(target))
        if original!=entry:run('/usr/bin/install_name_tool','-id','@loader_path/'+target.name,str(target))
        run('/usr/bin/codesign','--force','--sign','-',str(target))
        for ref in dependencies(target):
            if not system(ref) and not ref.startswith('@loader_path/'):
                raise ValueError('External dependency remains: '+ref)
    # Keep npm-relative requires intact. No external symlink may enter bundle.
    npm_dest=dest/'lib/node_modules/npm'
    shutil.copytree(npm,npm_dest,symlinks=True)
    count=0
    for path in npm_dest.rglob('*'):
        count+=1
        if count>15000:raise ValueError('npm package exceeds entry budget')
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(npm_dest.resolve()):
            raise ValueError('External npm symlink')
    (dest/'bin/npm').symlink_to('../lib/node_modules/npm/bin/npm-cli.js')
    (dest/'bin/npx').symlink_to('../lib/node_modules/npm/bin/npx-cli.js')
    # Node's distributed license includes bundled upstream notices; retain npm's
    # own license in the copied tree. This local toolchain is not a release.
    notices=dest/'notices';notices.mkdir()
    for candidate in (entry.parent.parent/'share/doc/node/LICENSE',entry.parent.parent/'LICENSE'):
        if candidate.is_file():shutil.copyfile(candidate,notices/'node-LICENSE');break
    (dest/'local-toolchain.json').write_text(json.dumps(dict(schema=1,local_acceptance_only=True,images=evidence),indent=2)+'\n')
    print(json.dumps(dict(destination=str(dest),images=len(edges),npm_files=count)))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--node',required=True);p.add_argument('--destination',required=True)
    a=p.parse_args();stage(a.node,a.destination)
