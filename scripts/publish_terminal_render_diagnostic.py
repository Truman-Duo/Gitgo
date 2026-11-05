"""Read-only Desktop TTY diagnostic, preserving the real stdout transport."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_acceptance_terminal import Console, console_client


def run(fixture):
    source = Path(__file__).resolve().parents[1]
    fixture = Path(fixture).resolve(strict=True)
    if not fixture.is_relative_to((Path.home() / "Desktop").resolve()):
        raise ValueError("Diagnostic artifacts must remain on Desktop")
    root = Path(tempfile.mkdtemp(prefix="render-ab-", dir=fixture))
    bun = source / "alpha3-work/tools/bun-windows-x64/bun.exe"
    entry = root / "entry.ts"
    entry.write_text("""import {appendFileSync, writeFileSync} from 'node:fs';
import instances from INSTANCES;
import {cellAt} from SCREEN;
const output = process.env.GITGO_RENDER_EVIDENCE!;
const originalWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = function(chunk: any, ...args: any[]) {
  appendFileSync(output + '/stdout.raw', typeof chunk === 'string' ? Buffer.from(chunk, typeof args[0] === 'string' ? args[0] : 'utf8') : chunk);
  appendFileSync(output + '/writes.jsonl', JSON.stringify({at:performance.now(), text: typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8')})+'\\n');
  return originalWrite(chunk, ...args);
} as typeof process.stdout.write;
setInterval(() => {
  const frame = (instances.get(process.stdout) as any)?.frontFrame;
  if (!frame?.screen) return;
  const screen=frame.screen, rows=[], widths=[];
  for(let y=0;y<screen.height;y++) {
    let line=''; const cells=[];
    for(let x=0;x<screen.width;x++) { const c=cellAt(screen,x,y); line+=c?.char || ''; cells.push([c?.char,c?.width]); }
    rows.push(line); widths.push(cells);
  }
  writeFileSync(output + '/frame-latest.json', JSON.stringify({at:performance.now(),width:screen.width,height:screen.height,rows,cells:widths}));
}, 200).unref();
await import(MAIN);
""".replace("INSTANCES", json.dumps((source / "cli/dashboard/vendor/ink/src/core/instances.ts").as_posix()))
        .replace("SCREEN", json.dumps((source / "cli/dashboard/vendor/ink/src/core/screen.ts").as_posix()))
        .replace("MAIN", json.dumps((source / "cli/dashboard/src/main.tsx").as_posix())), encoding="utf8")
    builder = root / "build.ts"
    builder.write_text("import {build} from 'bun'; import {readFileSync} from 'node:fs';\n"
        + "const result=await build({entrypoints:[" + json.dumps(str(entry)) + "],outdir:"
        + json.dumps(str(root / "bundle")) + ",target:'node',format:'esm',minify:false,tsconfig:"
        + json.dumps(str(source / "cli/dashboard/tsconfig.json")) + ",plugins:[{name:'diagnostic-metrics-toggle',setup(b){b.onLoad({filter:/[/\\\\]src[/\\\\]main\\.tsx$/},args=>{"
        + "let contents=readFileSync(args.path,'utf8'); const call='configureTerminalWidths(terminals?.display_metrics?.kind === \"windows_console\" ? terminals.display_metrics.codepoint_widths : undefined);';"
        + "if(!contents.includes(call)) throw Error('metrics injection anchor missing');"
        + "contents=contents.replace(call, `require('node:fs').writeFileSync(process.env.GITGO_RENDER_EVIDENCE + '/measured-metrics.json', JSON.stringify(terminals?.display_metrics)); if(process.env.GITGO_RENDER_NO_WIDTHS !== '1') ${call}`);"
        + "return {contents,loader:'tsx'};});}}]}); if(!result.success){console.error(result.logs);process.exit(1)};console.log('Desktop diagnostic built');\n", encoding="utf8")
    subprocess.run([str(bun), str(builder)], cwd=source / "cli/dashboard", check=True)
    reports = []
    project = json.loads((fixture / "config.json").read_text(encoding="utf8"))["projects"][0]["name"]
    for no_widths in (False, True):
        evidence = root / ("without-widths" if no_widths else "with-widths")
        evidence.mkdir()
        env = dict(os.environ, GITGO_STATE_HOME=str(fixture / "state"), GITGO_CONFIG_PATH=str(fixture / "config.json"),
            GITGO_LLM_CONFIG_PATH=str(fixture / "llm.json"), GITGO_PYTHON=str(fixture / "runtime/python.exe"),
            GITGO_INSTALL_ROOT=str(source), GITGO_RENDER_EVIDENCE=str(evidence),
            GITGO_RENDER_NO_WIDTHS="1" if no_widths else "0", PYTHONUTF8="1", FORCE_COLOR="3", TERM="xterm-256color")
        env.pop("NO_COLOR", None)
        launcher = evidence / "launch.py"
        launcher.write_text("import subprocess\nsubprocess.run(" + repr([str(bun), str(root / "bundle/entry.js"), "--attached"]) + ")\n", encoding="utf8")
        startup = subprocess.STARTUPINFO()
        startup.dwFlags, startup.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 1
        process = subprocess.Popen([str(Path(os.environ["SystemRoot"]) / "System32/conhost.exe"), sys.executable, str(launcher)],
            cwd=source, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        console = None
        report = {"root":str(evidence), "widths_enabled":not no_widths, "diagnostic_only":True}
        print(json.dumps(report), flush=True)
        try:
            console=Console(process,evidence,console_client(process),visible=True)
            console.resize(120,32)
            console.wait(project.partition("🚀")[0],"project")
            def capture(name):
                console.capture(name)
                console.screenshot(name)
                latest=evidence/'frame-latest.json'
                if latest.exists():
                    (evidence/(name+'-frame.json')).write_bytes(latest.read_bytes())
            capture('project-stable')
            console.send('\r')
            time.sleep(3)
            console.command('publish')
            console.send('\r')
            console.wait('本地已保存记录','publish')
            capture('publish-stable')
            console.send('\r')
            console.send('\r')
            console.wait('最终内容树','details')
            capture('120-details')
            for width,height in ((80,24),(40,20)):
                console.resize(width,height)
                capture(f'{width}x{height}-top')
                for _ in range(24): console.key(virtual=40)
                time.sleep(1)
                capture(f'{width}x{height}-scroll')
            report['completed']=True
        except BaseException as exc:
            report.update(error=type(exc).__name__,detail=str(exc))
            if console:
                console.capture('failure')
                console.screenshot('failure')
        finally:
            if console:
                report['initial_console_modes']=console.initial_console_modes
                report['resize_modes']=console.resize_modes
                console.close()
            elif process.poll() is None:
                subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,stdin=subprocess.DEVNULL)
            (evidence/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            reports.append(report)
    (root/'result.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'root':str(root),'reports':reports}),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('fixture')
    run(parser.parse_args().fixture)
