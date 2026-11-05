"""Read-only visible acceptance of the final packaged Windows executables."""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_acceptance_terminal import Console, PROCESSENTRY, console_client


def descendants(parent):
    kernel=C.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes, kernel.CreateToolhelp32Snapshot.restype=[W.DWORD,W.DWORD],W.HANDLE
    kernel.Process32FirstW.argtypes=kernel.Process32NextW.argtypes=[W.HANDLE,C.POINTER(PROCESSENTRY)]
    kernel.OpenProcess.argtypes,kernel.OpenProcess.restype=[W.DWORD,W.BOOL,W.DWORD],W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,C.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    rows=[]
    try:
        row=PROCESSENTRY(); row.size=C.sizeof(row)
        more=kernel.Process32FirstW(snapshot,C.byref(row))
        while more:
            rows.append({'pid':row.pid,'parent_pid':row.parent,'name':row.executable})
            more=kernel.Process32NextW(snapshot,C.byref(row))
    finally:
        kernel.CloseHandle(snapshot)
    owned={parent}
    while True:
        expanded=owned|{r['pid'] for r in rows if r['parent_pid'] in owned}
        if expanded==owned: break
        owned=expanded
    result=[r for r in rows if r['pid'] in owned]
    for row in result:
        handle=kernel.OpenProcess(0x1000,False,row['pid'])
        if handle:
            try:
                path=C.create_unicode_buffer(32768); size=W.DWORD(len(path))
                if kernel.QueryFullProcessImageNameW(handle,0,path,C.byref(size)): row['path']=path.value
            finally: kernel.CloseHandle(handle)
    return result


def probe_host(host,env,evidence):
    probe_env=dict(env,GITGO_STATE_HOME=str(evidence/'probe-state'),GITGO_CONFIG_PATH=str(evidence/'probe-config.json'),
        GITGO_LLM_CONFIG_PATH=str(evidence/'probe-llm.json'))
    shutil.copy2(env['GITGO_CONFIG_PATH'],probe_env['GITGO_CONFIG_PATH'])
    process=subprocess.Popen([str(host)],cwd=host.parent,env=probe_env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,text=True,encoding='utf8',creationflags=subprocess.CREATE_NO_WINDOW)
    messages=queue.Queue(); received=[]; errors=[]
    def read_output():
        for line in process.stdout:
            messages.put(json.loads(line))
    def read_errors():
        for line in process.stderr: errors.append(line)
    threading.Thread(target=read_output,daemon=True).start()
    threading.Thread(target=read_errors,daemon=True).start()
    try:
        start=time.monotonic()
        while True:
            message=messages.get(timeout=30); received.append(message)
            if message.get('type')=='host_started': break
            if message.get('type')=='host_rejected': raise RuntimeError(message)
        requested=time.monotonic()
        process.stdin.write(json.dumps({'protocol_version':1,'type':'request','request_id':'packaged-metrics',
            'operation':'config.terminals','arguments':{}})+'\n'); process.stdin.flush()
        while True:
            message=messages.get(timeout=35); received.append(message)
            if message.get('request_id')=='packaged-metrics':
                if message.get('error'): raise RuntimeError(message['error'])
                metrics=message['result']['display_metrics']
                assert metrics and metrics['kind']=='windows_console',message
                return {'host_executable':str(host),'host_pid':process.pid,'metrics':metrics,
                    'request_elapsed_ms':round((time.monotonic()-requested)*1000,2),'total_elapsed_ms':round((time.monotonic()-start)*1000,2)}
    finally:
        process.stdin.close()
        try: process.wait(timeout=15)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        (evidence/'host-protocol.json').write_text(json.dumps(received,ensure_ascii=False,indent=2),encoding='utf8')
        (evidence/'host-stderr.txt').write_text(''.join(errors),encoding='utf8')


def run(package,fixture,evidence_parent,*,pre_enable_vt=False,delayed_enable_vt=0.0,attach_delay=0.0):
    desktop=(Path.home()/'Desktop').resolve()
    package,fixture,evidence_parent=(Path(p).resolve() for p in (package,fixture,evidence_parent))
    if not all(p.is_relative_to(desktop) for p in (package,fixture,evidence_parent)):
        raise ValueError('Package, fixture and evidence must remain on Desktop')
    exe=package/'gitgo.exe'; host=package/'internal/gitgo-host/gitgo-host.exe'
    assert exe.is_file() and host.is_file()
    evidence_parent.mkdir(exist_ok=True)
    evidence=Path(tempfile.mkdtemp(prefix='review-',dir=evidence_parent))
    env={key:value for key,value in os.environ.items() if not key.upper().startswith(('GITGO_','PYTHON'))}
    env.update(GITGO_INSTALL_ROOT=str(package),GITGO_STATE_HOME=str(fixture/'state'),
        GITGO_CONFIG_PATH=str(fixture/'config.json'),GITGO_LLM_CONFIG_PATH=str(fixture/'llm.json'),
        FORCE_COLOR='3',TERM='xterm-256color')
    env.pop('NO_COLOR',None)
    launcher=evidence/'launch.py'
    prelude=''
    if pre_enable_vt or delayed_enable_vt:
        prelude="import ctypes as C,json\nfrom ctypes import wintypes as W\nk=C.WinDLL('kernel32',use_last_error=True)\nk.GetStdHandle.argtypes,k.GetStdHandle.restype=[W.DWORD],W.HANDLE\nk.GetConsoleMode.argtypes=[W.HANDLE,C.POINTER(W.DWORD)]\nk.SetConsoleMode.argtypes=[W.HANDLE,W.DWORD]\nh=k.GetStdHandle(-11); m=W.DWORD(); assert k.GetConsoleMode(h,C.byref(m)); before=m.value; assert k.SetConsoleMode(h,m.value|5); assert k.GetConsoleMode(h,C.byref(m))\nopen("+repr(str(evidence/'launch-console-modes.json'))+",'w').write(json.dumps({'before':before,'after':m.value}))\n"
    if delayed_enable_vt:
        launcher.write_text('import subprocess,time\np=subprocess.Popen('+repr([str(exe),'--attached'])+')\ntime.sleep('+repr(delayed_enable_vt)+')\n'+prelude+'p.wait()\n',encoding='utf8')
    else:
        launcher.write_text(prelude+'import subprocess\nsubprocess.run('+repr([str(exe),'--attached'])+')\n',encoding='utf8')
    startup=subprocess.STARTUPINFO(); startup.dwFlags,startup.wShowWindow=subprocess.STARTF_USESHOWWINDOW,1
    process=subprocess.Popen([str(Path(os.environ['SystemRoot'])/'System32/conhost.exe'),sys.executable,str(launcher)],
        cwd=package,env=env,creationflags=subprocess.CREATE_NEW_CONSOLE,startupinfo=startup)
    console=None
    report={'root':str(evidence),'business_result':str(fixture/'result.json'),'package_root':str(package),'read_only':True,
        'visual_review':'pending','diagnostic_pre_enable_vt':pre_enable_vt,'diagnostic_delayed_enable_vt_seconds':delayed_enable_vt,'diagnostic_attach_delay_seconds':attach_delay,'product_executable':str(exe),'host_executable':str(host),
        'product_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),'host_sha256':hashlib.sha256(host.read_bytes()).hexdigest(),
        'product_environment_overrides':{k:v for k,v in env.items() if k.startswith('GITGO_')},'images':[]}
    print(json.dumps({'evidence':str(evidence)}),flush=True)
    try:
        if attach_delay:
            time.sleep(attach_delay)
        console=Console(process,evidence,console_client(process),visible=True)
        console.resize(120,32)
        project=json.loads((fixture/'config.json').read_text(encoding='utf8'))['projects'][0]['name']
        console.wait(project.partition('🚀')[0],'project',timeout=60)
        report['process_tree']=descendants(process.pid)
        product_rows=[r for r in report['process_tree'] if r.get('path','').casefold()==str(exe).casefold()]
        host_rows=[r for r in report['process_tree'] if r.get('path','').casefold()==str(host).casefold()]
        assert product_rows and host_rows,report['process_tree']
        def capture(name):
            console.capture(name); report['images'].append(console.screenshot(name))
        capture('project-stable')
        before_screen,before_metrics=console.screen(),console.metrics()
        report['packaged_host_probe']=probe_host(host,env,evidence)
        after_screen,after_metrics=console.screen(),console.metrics()
        report['packaged_host_probe'].update(active_screen_unchanged=before_screen==after_screen,
            font_unchanged=(before_metrics['font'],before_metrics['font_size'])==(after_metrics['font'],after_metrics['font_size']))
        console.send('\r'); time.sleep(3); console.command('publish'); console.send('\r')
        console.wait('本地已保存记录','publish'); capture('publish-stable')
        console.send('\r'); console.send('\r'); console.wait('最终内容树','details')
        for width,height in ((120,32),(80,24),(40,20)):
            console.resize(width,height)
            for _ in range(150): console.key(virtual=38)
            time.sleep(.7); capture(f'{width}x{height}-top')
            for _ in range(24): console.key(virtual=40)
            time.sleep(.7); capture(f'{width}x{height}-scroll')
        report['capture_completed']=True
    except BaseException as exc:
        report.update(error=type(exc).__name__,detail=str(exc))
        if console:
            console.capture('failure'); report['images'].append(console.screenshot('failure'))
        raise
    finally:
        if console:
            report['initial_console_modes']=console.initial_console_modes
            report['resize_modes']=console.resize_modes
            console.close()
        elif process.poll() is None:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,stdin=subprocess.DEVNULL)
        (evidence/'visual_review.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        (evidence_parent/'latest.txt').write_text(str(evidence),encoding='utf8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--package',required=True); parser.add_argument('--fixture',required=True); parser.add_argument('--evidence',required=True)
    parser.add_argument('--pre-enable-vt',action='store_true',help='Diagnostic control: enable console VT output before launching the unchanged executable')
    parser.add_argument('--delayed-enable-vt',type=float,default=0.0,help='Diagnostic control: enable console VT after the unchanged executable has already started')
    parser.add_argument('--attach-delay',type=float,default=0.0,help='Delay the observation driver until startup has completed; do not change terminal modes')
    args=parser.parse_args(); run(args.package,args.fixture,args.evidence,pre_enable_vt=args.pre_enable_vt,delayed_enable_vt=args.delayed_enable_vt,attach_delay=args.attach_delay)
