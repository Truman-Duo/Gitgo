"""Observe early console-mode/text transitions without changing console modes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from publish_acceptance_terminal import Console, console_client

root=Path.home()/'Desktop/Gitgo-Issue40-Acceptance'
fixture=root/'terminal-m2-m3-9deslpx5'
package=root/'package-stage-delivery'
evidence=Path(tempfile.mkdtemp(prefix='startup-trace-',dir=root/'packaged-visual-review'))
env={k:v for k,v in os.environ.items() if not k.upper().startswith(('GITGO_','PYTHON'))}
env.update(GITGO_INSTALL_ROOT=str(package),GITGO_STATE_HOME=str(fixture/'state'),GITGO_CONFIG_PATH=str(fixture/'config.json'),
    GITGO_LLM_CONFIG_PATH=str(fixture/'llm.json'),FORCE_COLOR='3',TERM='xterm-256color')
env.pop('NO_COLOR',None)
print(str(evidence),flush=True)
for trial in range(3):
    target=evidence/str(trial);target.mkdir()
    gate=target/'start'
    launcher=target/'launch.py'
    launcher.write_text('import subprocess,time,pathlib\np=pathlib.Path('+repr(str(gate))+')\nwhile not p.exists():time.sleep(.005)\nsubprocess.run('+repr([str(package/'gitgo.exe'),'--attached'])+')\n',encoding='utf8')
    startup=subprocess.STARTUPINFO();startup.dwFlags,startup.wShowWindow=subprocess.STARTF_USESHOWWINDOW,1
    process=subprocess.Popen([str(Path(os.environ['SystemRoot'])/'System32/conhost.exe'),sys.executable,str(launcher)],cwd=package,
        env=env,creationflags=subprocess.CREATE_NEW_CONSOLE,startupinfo=startup)
    console=None;events=[]
    try:
        console=Console(process,target,console_client(process),visible=True)
        console.resize(120,32)
        initial=console.modes();started=time.monotonic();gate.write_text('start',encoding='ascii')
        previous=None
        while time.monotonic()-started<7:
            modes=console.modes();screen=console.screen()[0]
            state=(modes['input'],modes['output'],screen)
            if state!=previous:
                events.append({'elapsed_ms':round((time.monotonic()-started)*1000,2),'modes':modes,'screen':screen})
                previous=state
            time.sleep(.005)
        console.capture('startup');console.screenshot('startup')
        report={'initial':initial,'events':events,'has_fragment':any('1KLE' in e['screen'] for e in events)}
        (target/'trace.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps({'trial':trial,'initial':initial,'events':[{**e,'screen':e['screen'][:120]} for e in events],'has_fragment':report['has_fragment']},ensure_ascii=True),flush=True)
    finally:
        if console:console.close()
        elif process.poll() is None:subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
