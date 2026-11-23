"""Install an immutable local helper build; verify Git normal and fault paths.

Explicit sudo operator acceptance only. Does not replace the normal Gitgo app,
change PATH, alter user projects, or switch an existing persistent service.
Installs a content-addressed ad-hoc helper version, then uses temporary jobs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from check_macos_execution_chain import stage_runtime
SUPPORT=('check_macos_git_broker.py','check_macos_git_broker_faults.py','macos_git_packaged_support.py',
         'check_macos_user_broker.py','check_macos_git_read.py','check_macos_helper_lifecycle.py','check_macos_worktree_client.py','check_macos_promotion_client.py')
if __name__=='__main__' and '--run' in sys.argv and '--staged' not in sys.argv and os.geteuid()==0:
    stage_runtime(__file__,support=SUPPORT)
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true');parser.add_argument('--staged',action='store_true')
    parser.add_argument('--bundle',required=True);parser.add_argument('--allow-local-adhoc',action='store_true')
    parser.add_argument('--consumers',action='store_true',help='Also exercise actual Git consumer adapters and ancestry predicates')
    parser.add_argument('--with-lifecycle',action='store_true',help='Then test an isolated resident service install, upgrade, rollback and uninstall')
    parser.add_argument('--objects',action='store_true',help='Exercise private blob/tree/commit production and verified Host append')
    parser.add_argument('--checkout',action='store_true',help='Also verify commit images; fault cases then exercise checkout instead of object writes')
    parser.add_argument('--worktrees',action='store_true',help='Exercise durable linked-worktree publication and Host crash recovery')
    parser.add_argument('--finalize',action='store_true')
    parser.add_argument('--only-stage',choices=('finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review'),help='Focused acceptance; does not claim the other stages passed')
    parser.add_argument('--runtime-merge',action='store_true')
    parser.add_argument('--seal',action='store_true',help='Also verify explicit result seal acknowledgement and crash recovery')
    parser.add_argument('--compare',action='store_true',help='Verify immutable tree and changed-path consumers')
    parser.add_argument('--results',action='store_true',help='Verify privacy-gated immutable result refs')
    parser.add_argument('--index',action='store_true',help='Also verify protected index writes and abrupt Host death')
    args=parser.parse_args()
    if args.only_stage and not args.finalize:parser.error('--only-stage requires --finalize')
    if args.only_stage and args.with_lifecycle:parser.error('--only-stage cannot be combined with --with-lifecycle')
    if args.finalize and not (args.runtime_merge and args.seal):parser.error('--finalize requires --runtime-merge --seal')
    if args.runtime_merge and not args.seal:parser.error('--runtime-merge requires --seal')
    if args.seal and not args.results:parser.error('--seal requires --results')
    if args.compare and not (args.checkout and args.objects and args.consumers):parser.error('--compare requires --checkout --objects --consumers')
    if args.results and (not args.worktrees or args.index):parser.error('--results requires --worktrees without --index')
    if args.index and not args.worktrees:parser.error('--index requires --worktrees')
    if args.worktrees and not (args.checkout and args.consumers):parser.error('--worktrees requires --checkout and --consumers')
    if args.checkout and not args.objects:parser.error('--checkout requires --objects for the nonempty normal fixture')
    if not args.run or not args.staged or sys.platform!='darwin' or os.getuid()!=0 or os.geteuid()!=0 or not 501<=int(os.environ.get('SUDO_UID','0'))<200000000:
        parser.error('Use sudo --run from your ordinary login account')
    from backend.core.macos_helper_install import install,verify_bundle_path
    report=dict(passed=False,production_enabled=False,consumers=args.consumers,with_lifecycle=args.with_lifecycle,
        objects=args.objects,checkout=args.checkout,worktrees=args.worktrees,index=args.index,results=args.results,compare=args.compare,seal=args.seal,runtime_merge=args.runtime_merge,finalize=args.finalize,normal=None,faults=None,runtime=str(Path(__file__).resolve().parents[1]))
    try:
        helper=install(Path(args.bundle).absolute(),allow_local_adhoc=args.allow_local_adhoc)
        version=verify_bundle_path(helper)
        report.update(helper=str(helper),bundle_id=version.name,host_sha256=hashlib.sha256(helper.read_bytes()).hexdigest())
        print('Installed immutable helper: '+str(helper),file=sys.stderr,flush=True)
        stages=[('normal','check_macos_git_broker.py'),('faults','check_macos_git_broker_faults.py')]
        if args.results:stages.insert(0,('worktree_lifecycle','check_macos_git_broker.py'))
        if args.seal and not args.finalize:stages.insert(-1,('sealing','check_macos_git_broker.py'))
        if args.runtime_merge and not args.finalize:stages.insert(-1,('runtime_merge','check_macos_git_broker.py'))
        if args.finalize:stages[-1:-1]=[('finalize_pending','check_macos_git_broker.py'),('finalize_ack','check_macos_git_broker.py'),('finalize_boundaries','check_macos_git_broker.py'),('finalize_upstream','check_macos_git_broker.py'),('finalize_resume','check_macos_git_broker.py'),('finalize_promotion_read','check_macos_git_broker.py')]
        if args.only_stage=='finalize_promotion_review':stages.append(('finalize_promotion_review','check_macos_git_broker.py'))
        report['split_worktree_batch']=args.results
        if args.with_lifecycle:stages.append(('lifecycle','check_macos_helper_lifecycle.py'))
        if args.only_stage:stages=[s for s in stages if s[0]==args.only_stage]
        report['selected_stages']=[key for key,_ in stages]
        report['focused_acceptance']=bool(args.only_stage)
        for key,name in stages:
            print('Packaged Git acceptance: '+key,file=sys.stderr,flush=True)
            # Harness owns its bounded children and cleanup. Do not impose an
            # outer timeout that could kill it before it archives a live lease.
            options=(['--bundle',str(Path(args.bundle).absolute()),*(['--allow-local-adhoc'] if args.allow_local_adhoc else [])]
                if key=='lifecycle' else ['--helper',str(helper),*(['--consumers'] if args.consumers else []),*(['--objects'] if args.objects else [])])
            if args.checkout and key!='lifecycle':
                if key=='faults':options=['--helper',str(helper)]
                options.append('--checkout')
            if args.worktrees and key in ('normal','worktree_lifecycle','sealing','runtime_merge','finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review'):options.append('--worktrees')
            if args.compare and key==('worktree_lifecycle' if args.results else 'normal'):options.append('--compare')
            if args.results and key in ('normal','sealing','runtime_merge','finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review'):options.append('--results')
            if key in ('sealing','runtime_merge','finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review'):options.append('--seal')
            if key=='runtime_merge' or key.startswith('finalize_'):options.append('--runtime-merge')
            if key.startswith('finalize_'):options.extend(['--finalize',key.removeprefix('finalize_')])
            if args.results and key=='faults':options=['--helper',str(helper),'--objects']
            if args.index:
                if key=='normal':options.append('--index')
                elif key=='faults':options=['--helper',str(helper),'--objects']
            result=subprocess.run([sys.executable,'-B','-I',str(Path(__file__).with_name(name)),
                '--run','--staged',*options],capture_output=True,text=True)
            report[key+'_exit_code']=result.returncode
            report[key+'_stderr']=result.stderr[-8000:]
            try: report[key]=json.loads(result.stdout)
            except ValueError:
                report[key+'_raw']=result.stdout[-16000:]
                raise RuntimeError(key+' acceptance did not return a JSON report')
            if (result.returncode or report[key].get('passed') is not True
                    or key!='lifecycle' and report[key].get('frozen_package_verified') is not True):
                raise RuntimeError(key+' packaged acceptance failed; retain its diagnostics and recovery evidence')
            if key in ('normal','worktree_lifecycle','sealing','runtime_merge','finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review') and args.worktrees and report[key].get('worktrees') is not True:
                raise RuntimeError('normal did not exercise linked-worktree lifecycle')
            if args.compare and key==('worktree_lifecycle' if args.results else 'normal') and report[key].get('compare') is not True:
                raise RuntimeError('commit comparisons were not exercised')
            if key.startswith('finalize_') and report[key].get('finalize')!=key.removeprefix('finalize_'):raise RuntimeError('automatic finalization was not exercised')
            if (key=='runtime_merge' or key.startswith('finalize_')) and report[key].get('runtime_merge') is not True:raise RuntimeError('runtime merge was not exercised')
            if key in ('sealing','runtime_merge','finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review') and report[key].get('seal') is not True:
                raise RuntimeError('explicit seal acknowledgement was not exercised')
            if key in ('normal','sealing','runtime_merge','finalize_pending','finalize_ack','finalize_boundaries','finalize_upstream','finalize_resume','finalize_promotion_read','finalize_promotion_review') and args.results and report[key].get('results') is not True:
                raise RuntimeError('normal did not exercise result publication')
            if key=='normal' and args.index and report[key].get('index') is not True:
                raise RuntimeError('normal did not exercise protected index staging')
            if key=='faults' and args.index and report[key].get('objects') is not True:
                raise RuntimeError('faults did not exercise index object producers')
            if key!='lifecycle' and not ((args.index or args.results) and key=='faults') and args.checkout and report[key].get('checkout') is not True:
                raise RuntimeError(key+' did not exercise checkout')
            if key!='lifecycle' and not (key=='faults' and args.checkout) and args.objects and report[key].get('objects') is not True:
                raise RuntimeError(key+' did not exercise object writes')
            if key!='lifecycle' and not (key=='faults' and args.checkout) and args.consumers and report[key].get('consumers') is not True:
                raise RuntimeError(key+' did not exercise consumer integration')
        report['bundle_unchanged']=verify_bundle_path(helper)==version
        report['passed']=report['bundle_unchanged']
    except Exception as exc:report['error']=type(exc).__name__+': '+str(exc)
    print(json.dumps(report,indent=2),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
