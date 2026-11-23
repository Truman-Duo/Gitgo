"""Pinned helper lifecycle acceptance in a disposable SIP-enabled VirtualMac.

Uses real frozen administrator/ordinary-client roles and real launchd. Keeps
root recovery records and uses only unique disposable images and projects.
Run --run with a bundle and pin, reboot the guest, then --resume REPORT. No production enablement.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime, ISOLATION
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=('check_macos_user_broker.py',))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core import macos_helper_install as install
from backend.core import macos_helper_lifecycle as life
from backend.core import macos_identity as identity
from backend.core.sandbox_macos import load_state
import check_macos_user_broker as harness


def require(ok,message):
    if not ok: raise RuntimeError(message)


def guard():
    require(sys.platform=='darwin' and os.getuid()==os.geteuid()==0,'Root in the disposable guest is required')
    require(subprocess.check_output(['/usr/sbin/sysctl','-n','hw.model'],text=True).startswith('VirtualMac'),
            'Refuse host machine acceptance')
    require(subprocess.check_output(['/usr/bin/csrutil','status'],text=True).strip()==
            'System Integrity Protection status: enabled.','SIP must be enabled')


def digest(bundle): return install.identity(install.inventory(bundle))


def pid(root):
    result=life.ctl('print','system/'+life.PREFIX+root.name)
    found=re.search(r'^\s*pid = (\d+)\s*$',result.stdout,re.M)
    return int(found[1]) if result.returncode==0 and found else None


def snapshot(root):
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
           for p in (root/'private').glob('lifecycle-*.json')}
    return dict(pid=pid(root),state=life.configuration(root),
                plist=hashlib.sha256(life.job_path(root).read_bytes()).hexdigest(),
                credential=hashlib.sha256((root/'client.json').read_bytes()).hexdigest(),journals=files)


def command(report,manager,*args,ok=True):
    result=subprocess.run([str(manager),'--gitgo-internal-role','macos-helper-admin',*map(str,args)],
        stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=210,
        env={'PATH':'/usr/bin:/bin','HOME':'/var/root','SUDO_UID':str(report['uid']),
             'PYTHONDONTWRITEBYTECODE':'1','LC_ALL':'C'})
    report['commands'].append(dict(arguments=list(map(str,args)),manager=str(manager),
        returncode=result.returncode,stdout=result.stdout[-16000:],stderr=result.stderr[-8000:]))
    require((result.returncode==0)==ok,'Unexpected administrator result: '+result.stderr[-2000:])
    return json.loads(result.stdout)['result'] if ok else result


def task(report,root):
    """An actual ordinary Host runs isolated writes/child/negative probes."""
    import socket
    project=Path(report['project']);home=project.parent;uid=report['uid'];owner=pwd.getpwuid(uid)
    canary=home.parent/'outside.txt';canary.write_text('private-canary');canary.chmod(0o666)
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(2)
    source="import sys\nsys.argv=['task',"+repr(str(canary))+","+repr(str(listener.getsockname()[1]))+"]\n"
    source+=ISOLATION+"\nPath('outside-link').unlink()\nPath('result.json').write_text(json.dumps(result))\n"
    path=project/'task.py';path.write_text(source);os.chown(path,uid,owner.pw_gid)
    helper=Path(life.configuration(root)['helper']);harness.HELPER=helper
    try:
        process=subprocess.run(harness.child_command(root/'client.json','success',home/'cancel',uid),
            user=uid,group=owner.pw_gid,extra_groups=[],cwd=home,stdin=subprocess.DEVNULL,
            capture_output=True,text=True,timeout=40,env={'PATH':'/usr/bin:/bin','HOME':str(home),
            'TMPDIR':str(home),'PYTHONDONTWRITEBYTECODE':'1','GITGO_STATE_HOME':str(home/'state'),
            'GITGO_CONFIG_PATH':str(home/'config.json'),'GITGO_LLM_CONFIG_PATH':str(home/'llm.json')})
    finally: listener.close()
    require(process.returncode==0,'Ordinary client failed: '+process.stderr[-2000:])
    response=json.loads(process.stdout)
    require(response['runtime']['frozen'] and response['uid']==uid,'Wrong client identity')
    require(not response['result']['is_error'],'Task failed: '+str(response['result']))
    active=json.loads(identity.bounded_read(root/'private/active.json',0,True));lease=Path(active['lease'])
    state=harness.wait_for(lambda:(s if (s:=load_state(lease))['phase']=='clean' else None),18,'UID not clean')
    facts=json.loads((project/'result.json').read_text())
    require(state['launch_count']==1 and state['cleanup']['observed_empty'] and not state['cleanup']['issues']
            and not identity.owned_rows(state['uid']),'Cleanup not confirmed')
    require(facts['uid']==state['uid'] and all(v for k,v in facts.items() if k!='uid'),'Isolation failure')
    require(canary.read_text()=='private-canary','Outside file changed')
    report['tasks'].append(dict(helper=str(helper),lease=str(lease),invocation_id=state['invocation_id'],
        launch_count=state['launch_count'],uid=state['uid'],clean=True,facts=facts))


def prepare(args):
    uid=int(os.environ.get('SUDO_UID','0'));require(501<=uid<identity.UID_MIN,'Expected guest operator')
    owner=pwd.getpwuid(uid);bundle=Path(args.bundle).resolve(strict=True)
    require(digest(bundle)==args.expected_bundle_sha256,'Independent build pin mismatch')
    # macOS clears /private/tmp on reboot. Retain the same project and its
    # authorization identity until the post-reboot check has finished.
    base=Path(tempfile.mkdtemp(prefix='gitgo-pin-check-',dir='/private/var/tmp'));base.chmod(0o711)
    home=base/'user';home.mkdir(mode=0o700);os.chown(home,uid,owner.pw_gid)
    project=home/'project';project.mkdir(mode=0o700);os.chown(project,uid,owner.pw_gid)
    name='pin-'+uuid.uuid4().hex[:20];root=life.service_root(name)
    report=dict(schema=1,passed=False,prepared=False,service=name,uid=uid,project=str(project),
        base=str(base),boot=identity.boot_id(),initial_pin=args.expected_bundle_sha256,
        commands=[],checks={},tasks=[],production_enabled=False)
    report_path=base/'report.json'
    manager=bundle/'gitgo-host'
    try:
        command(report,manager,'install','--name',name,'--bundle',bundle,'--project',project,
                '--expected-bundle-sha256','0'*64,ok=False)
        require(not root.exists(),'Bad pin created service state')
        report['checks']['wrong_pin_no_service']=True
        current=command(report,manager,'install','--name',name,'--bundle',bundle,'--project',project,
            '--lifetime','3600','--expected-bundle-sha256',args.expected_bundle_sha256)
        manager=Path(current['helper']);report['initial_helper']=str(manager)
        require(install.read_receipt(manager.parent.parent)['schema']==2,'Missing pinned receipt')
        task(report,root)
        candidate=base/'candidate';shutil.copytree(bundle,candidate,symlinks=True)
        (candidate/'_internal/pin-acceptance-marker.txt').write_text(name+' B\n')
        candidate_pin=digest(candidate);before=snapshot(root)
        command(report,manager,'upgrade','--name',name,'--bundle',candidate,ok=False)
        require(snapshot(root)==before,'Missing pin changed service')
        command(report,manager,'upgrade','--name',name,'--bundle',candidate,'--allow-local-adhoc',ok=False)
        require(snapshot(root)==before,'Legacy trust downgraded service')
        report['checks']['no_unpinned_downgrade']=True
        command(report,manager,'upgrade','--name',name,'--bundle',candidate,
                '--expected-bundle-sha256',args.expected_bundle_sha256,ok=False)
        require(snapshot(root)==before,'Wrong upgrade pin changed service')
        current=command(report,manager,'upgrade','--name',name,'--bundle',candidate,
                '--expected-bundle-sha256',candidate_pin)
        manager=Path(current['helper']);report['current_helper']=str(manager);report['current_pin']=candidate_pin
        require(current['previous']==report['initial_helper'],'Lost exact predecessor')
        require(install.read_receipt(manager.parent.parent)['schema']==2,'Candidate not adopted with pin')
        task(report,root);report['checks']['pinned_upgrade']=True
        # Only the unique acceptance marker is changed; it is restored even if
        # the expected refusal fails. Never alter a previous real program image.
        marker=manager.parent/'_internal/pin-acceptance-marker.txt';raw=marker.read_bytes();before=snapshot(root)
        try:
            marker.write_bytes(raw+b'tamper\n')
            command(report,manager,'status','--name',name,ok=False)
            command(report,bundle/'gitgo-host','recover','--name',name,ok=False)
            require(snapshot(root)==before,'Tampered runtime stopped service or rewrote state')
        finally: marker.write_bytes(raw)
        install.verify_bundle_path(manager);report['checks']['tamper_rejected_before_recovery_stop']=True
        # Real signed inert executable, not a mock of a startup failure.
        broken=base/'broken';broken.mkdir();shutil.copyfile('/usr/bin/false',broken/'gitgo-host')
        (broken/'gitgo-host').chmod(0o755)
        subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(broken/'gitgo-host')],check=True,capture_output=True)
        (broken/'intent.txt').write_text(name+' inert startup failure\n')
        command(report,manager,'upgrade','--name',name,'--bundle',broken,
                '--expected-bundle-sha256',digest(broken),ok=False)
        state=life.configuration(root)
        require(state['phase']=='active' and state['helper']==str(manager),'Failed startup did not restore predecessor')
        task(report,root);report['checks']['failed_startup_restores_exact_pinned_helper']=True
        # Kill this run's exact administrator while its durable switch is in
        # flight. Normal recover must choose the journaled predecessor.
        interrupted=subprocess.Popen([str(manager),'--gitgo-internal-role','macos-helper-admin',
            'upgrade','--name',name,'--bundle',str(broken),'--expected-bundle-sha256',digest(broken)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            env={'PATH':'/usr/bin:/bin','HOME':'/var/root','SUDO_UID':str(uid)})
        try:
            harness.wait_for(lambda:life.configuration(root)['phase']=='switching',20,'No durable switch')
            interrupted.kill();interrupted.wait(timeout=10)
        finally:
            if interrupted.poll() is None: interrupted.kill();interrupted.wait(timeout=10)
        command(report,manager,'recover','--name',name)
        require(life.configuration(root)['helper']==str(manager),'Recovery selected another image')
        task(report,root);report['checks']['manager_crash_recovers_recorded_predecessor']=True
        before=pid(root);require(bool(before),'Service not running')
        require(life.ctl('kill','SIGKILL','system/'+life.PREFIX+name).returncode==0,'Could not fault service')
        harness.wait_for(lambda:pid(root) not in (None,before),20,'launchd did not restart')
        life.ready(root);task(report,root);report['checks']['launchd_restart_with_pin']=True
        # Ordinary user cannot alter installed dependency or root trust receipt.
        for path in (marker,manager.parent.parent/'install.json'):
            probe=subprocess.run(['/bin/sh','-c','printf malicious >> "$1"','sh',str(path)],
                user=uid,group=owner.pw_gid,extra_groups=[],capture_output=True,timeout=5)
            require(probe.returncode!=0,'Ordinary operator altered root snapshot')
        install.verify_bundle_path(manager);report['checks']['ordinary_user_write_denied']=True
        report['prepared']=True
    finally:
        identity.atomic_json(report_path,report)
        print(json.dumps(dict(report=str(report_path),prepared=report['prepared']),indent=2),flush=True)
    return report


def resume(path):
    path=Path(path);report=json.loads(identity.bounded_read(path,0,True))
    require(report['prepared'] and re.fullmatch('pin-[0-9a-f]{20}',report['service']), 'Wrong acceptance report')
    require(path==Path(report['base'])/'report.json' and path.parent.parent==Path('/private/var/tmp')
            and path.parent.name.startswith('gitgo-pin-check-'),
            'Wrong acceptance location')
    require(identity.boot_id()!=report['boot'],'Guest reboot required')
    root=life.service_root(report['service']);manager=Path(report['current_helper'])
    require(life.configuration(root)['helper']==str(manager),'Service changed across reboot')
    harness.wait_for(lambda:pid(root),30,'Resident service did not start')
    life.ready(root);install.verify_bundle_path(manager)
    status=command(report,manager,'status','--name',root.name)
    require(status['loaded'] and not status['authorization_live'],'Boot unexpectedly renewed project authority')
    command(report,manager,'renew','--name',root.name,ok=False)
    command(report,manager,'renew','--name',root.name,'--after-reboot','--lifetime','600')
    task(report,root);report['checks']['reboot_retains_pin_requires_fresh_grant']=True
    command(report,manager,'uninstall','--name',root.name)
    require(life.ctl('print','system/'+life.PREFIX+root.name).returncode==113,'Service still registered')
    require(not life.job_path(root).exists() and not (root/'client.json').exists(),'Service files remain')
    command(report,manager,'uninstall','--name',root.name)
    require(all(not identity.owned_rows(t['uid']) for t in report['tasks']),'Task UID still alive')
    report['checks']['uninstall_idempotent_clean']=True
    report.update(passed=True,final_boot=identity.boot_id(),retained_service_state=life.configuration(root))
    identity.atomic_json(path,report)
    print(json.dumps(dict(passed=True,report=str(path),checks=report['checks'],tasks=len(report['tasks'])),indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--bundle');parser.add_argument('--expected-bundle-sha256');parser.add_argument('--resume')
    args=parser.parse_args();guard();require(args.run and args.staged,'Explicit staged acceptance required')
    if args.resume: resume(args.resume)
    else:
        require(args.bundle and args.expected_bundle_sha256,'Bundle and independent pin required')
        prepare(args)


if __name__=='__main__': main()
