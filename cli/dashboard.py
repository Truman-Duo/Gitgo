"""Dashboard TUI — gitgo 项目控制台
上下选项目 → Enter 进详情(Tab切换子面板) → :cmd 指令
"""

from __future__ import annotations
import json, sys, time
from pathlib import Path
from backend.core.config import Config
 ([GITGO-30] chore/docs/feat/fix/refactor/test(all,backend,cli,config,core,cui,dashboard,docs,frontend,governance,identity,knowledge,protocol,readme,remote,runtime,security,state,sync,template,tests): 工作区→备份仓库同步工具初始提交 +29 more)

# ── Data helpers ──────────────────────────────────────────
def _load_history(ws: str) -> list[dict]:
    hp = Path(ws) / "gitgo_history.json"
    if not hp.exists(): return []
    try: d = json.loads(hp.read_text(encoding="utf-8")); return d if isinstance(d,list) else d.get("entries",[])
    except: return []

def _last_gate(name: str, ws: str) -> dict:
    es = _load_history(ws); ps = [e for e in es if e.get("project_name")==name]; s=d=None
    for e in reversed(ps):
        op=e.get("operation",""); dt=e.get("detail",{}) if isinstance(e.get("detail"),dict) else {}
        if op=="governance_synced" and not s: s={"status":"passed","commit":dt.get("commit","?"),"time":e.get("timestamp","")[:19]}
        if op=="governance_drift" and not d: d={"status":"blocked","rules":dt.get("rules",[]),"time":e.get("timestamp","")[:19]}
        if s and d: break
    if not s and not d: return {"status":"idle"}
    if s and d: return s if s["time"]>d["time"] else d
    return s or d ([GITGO-30] chore/docs/feat/fix/refactor/test(all,backend,cli,config,core,cui,dashboard,docs,frontend,governance,identity,knowledge,protocol,readme,remote,runtime,security,state,sync,template,tests): 工作区→备份仓库同步工具初始提交 +29 more)

def _pending_count(ws: str, name: str) -> int:
    try: fp=Path(ws)/".gitgo"/"knowledge"/"instances"/name/"pending.jsonl"; return sum(1 for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()) if fp.exists() else 0
    except: return 0

def _load_pending(ws: str, name: str) -> list[dict]:
    try: fp=Path(ws)/".gitgo"/"knowledge"/"instances"/name/"pending.jsonl"; return [json.loads(l) for l in fp.read_text(encoding="utf-8").splitlines() if l.strip()] if fp.exists() else []
    except: return []

def _contract(ws: str) -> dict:
    from backend.core.contract import ContractManager
    c=ContractManager.load(Path(ws)); return {"features":len(c.decided_features),"constraints":len(c.architecture_constraints),"ts":c.tech_stack} if c else {"features":0,"constraints":0,"ts":[]}

def _fmt_gate(s: str) -> str:
    return {"passed":"[green]PASSED[/green]","blocked":"[red]BLOCKED[/red]"}.get(s,"[dim]IDLE[/dim]")

def _fmt_sev(s: str) -> str:
    return {"critical":"[red bold]","high":"[red]","medium":"[yellow]","low":"[dim]"}.get(s,"")

# ── Keyboard ──────────────────────────────────────────────
_msvcrt = None
def _init_kb():
    global _msvcrt
    try: import msvcrt; _msvcrt=msvcrt
    except: pass
_init_kb()

def _kbhit(): return _msvcrt.kbhit() if _msvcrt else False

def _getch():
    if not _msvcrt: return None
    ch=_msvcrt.getch()
    if ch in(b'\xe0',b'\x00'):
        ch2=_msvcrt.getch()
        return {b'H':'up',b'P':'down',b'K':'left',b'M':'right',b'I':'pgup',b'Q':'pgdn'}.get(ch2,None)
    if ch==b'\r': return 'enter'
    if ch==b'\t': return 'tab'
    if ch==b'\x08': return 'backspace'
    if ch==b'\x1b': return 'esc'
    if len(ch)==1:
        c=ch.decode("utf-8","ignore")
        return c if c.isprintable() or c==' ' else None
    return None

# ── Command handler ────────────────────────────────────────
def _handle_cmd(cfg, cmd):
    parts=cmd.strip().split()
    if not parts: return ""
    a=parts[0].lower()

    if a in("l","lesson"):
        name=parts[1] if len(parts)>1 else (cfg.projects[0].name if cfg.projects else "")
        p=next((x for x in cfg.projects if x.name==name),None)
        if not p: return "[red]Not found[/red]"
        ls=_load_pending(p.workspace_path,p.name)
        if not ls: return f"[dim]{name}: no pending lessons[/dim]"
        return "\n".join([f"[bold]{name} — {len(ls)} pending:[/bold]"]+[f"  [{l.get('severity','?')[:3]}] {l.get('trigger','')[:80]}" for l in ls[:10]])

    if a in("v","verify"):
        if len(parts)<2: return "[red]v <id>[/red]"
        from backend.core.knowledge.lesson import LessonManager
        for p in cfg.projects:
            r=LessonManager.verify(Path(p.workspace_path),parts[1],project_name=p.name)
            if r: return f"[green]Verified {parts[1][:30]} (count:{r.verified_count})[/green]"
        return f"[red]Not found: {parts[1][:30]}[/red]"

    if a in("c","contract"):
        name=parts[1] if len(parts)>1 else (cfg.projects[0].name if cfg.projects else "")
        p=next((x for x in cfg.projects if x.name==name),None)
        if not p: return "[red]Not found[/red]"
        from backend.core.contract import ContractManager
        c=ContractManager.load(Path(p.workspace_path))
        if not c: return "[dim]No contract[/dim]"
        return "\n".join([f"[bold]{name} Contract:[/bold]  Features:{len(c.decided_features)} Constraints:{len(c.architecture_constraints)}"]+[f"  [{f.confirmed_count}x] {f.name}" for f in c.decided_features]+[f"  [red]-[/red] {x}" for x in c.architecture_constraints])

    if a in("s","status"):
        name=parts[1] if len(parts)>1 else (cfg.projects[0].name if cfg.projects else "")
        p=next((x for x in cfg.projects if x.name==name),None)
        if not p: return "[red]Not found[/red]"
        g=_last_gate(p.name,p.workspace_path); pc=_pending_count(p.workspace_path,p.name); ct=_contract(p.workspace_path)
        return f"[bold]{p.name}[/bold]  Gate A:{_fmt_gate(g['status'])}  Commit:{g.get('commit','-')}  Lessons:{pc}  Features:{ct['features']}  Constraints:{ct['constraints']}"

    if a in("h","help"):
        return "[bold]Commands:[/bold] l[esson] c[ontract] s[tatus] v[erify] <id> h[elp]"

    return f"[red]Unknown: {cmd}[/red]"


# ── Main Dashboard ─────────────────────────────────────────

def cmd_dashboard(cfg: Config, refresh: int = 5) -> None:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.table import Table
    from rich.text import Text

    console = Console()
    sel = 0              # project index
    detail = False       # in project detail
    detail_tab = 0       # 0=Lessons 1=Contract 2=Events
    detail_sel = 0       # selected item in current tab
    cmd_buf = None       # None=nav, str=command mode
    cmd_result = ""      # feedback text
    cmd_history = []     # command history
    cmd_hist_idx = -1
    dirty = True

    # 缓存 overview 数据（定时刷新，上下键不触发 IO）
    _cache_data = []  # [(gate, pending, contract), ...]
    _cache_time = 0.0

    def _refresh_cache():
        nonlocal _cache_data, _cache_time
        _cache_data = [(_last_gate(p.name,p.workspace_path),_pending_count(p.workspace_path,p.name),_contract(p.workspace_path))
                       for p in cfg.projects if p.workspace_path]
        _cache_time = time.time()

    _refresh_cache()

    # ── Views ──────────────────────────────────────────────

    def _view_overview():
        t = Table(title="Gitgo Monitor",title_style="bold white",header_style="bold cyan")
        t.add_column("",width=2); t.add_column("Project",style="cyan",width=10)
        t.add_column("Gate A",width=18); t.add_column("Commit",width=14)
        t.add_column("Lessons",width=8,justify="right"); t.add_column("Contract",width=10); t.add_column("Last",width=20)
        for i,p in enumerate(cfg.projects):
            if not p.workspace_path: continue
            if i < len(_cache_data):
                g, pc, ct = _cache_data[i]
            else:
                g, pc, ct = _last_gate(p.name,p.workspace_path), _pending_count(p.workspace_path,p.name), _contract(p.workspace_path)
            t.add_row("▶" if i==sel else " ",p.name,_fmt_gate(g["status"]),g.get("commit","-"),
                       str(pc),f"{ct['features']}f/{ct['constraints']}c",g.get("time","-"))
        return t

    def _view_detail_tabs():
        """顶部 Tab 栏"""
        tabs = ["Lessons","Contract","Events"]
        parts = []
        for i,t in enumerate(tabs):
            parts.append(f"[bold cyan]{t}[/bold cyan]" if i==detail_tab else f"[dim]{t}[/dim]")
        return " │ ".join(parts) + "  [dim](←→ switch  ↑↓ select  Enter open)[/dim]"

    def _view_lessons():
        if not cfg.projects: return "[dim]No projects[/dim]"
        p = cfg.projects[min(sel,len(cfg.projects)-1)]
        lessons = _load_pending(p.workspace_path,p.name)
        if not lessons: return "[dim]No pending lessons[/dim]"
        lines = []
        for i,l in enumerate(lessons[:10]):
            marker = "▶" if i==detail_sel else " "
            sev = l.get("severity","medium")
            trigger = l.get("trigger","")[:65]
            lines.append(f" {marker} {_fmt_sev(sev)}[{sev[:3]}][/] [{l.get('category','?')}] {trigger}")
        if len(lessons) > 10:
            lines.append(f" [dim]... and {len(lessons)-10} more[/dim]")
        return "\n".join(lines) ([GITGO-30] chore/docs/feat/fix/refactor/test(all,backend,cli,config,core,cui,dashboard,docs,frontend,governance,identity,knowledge,protocol,readme,remote,runtime,security,state,sync,template,tests): 工作区→备份仓库同步工具初始提交 +29 more)

    def _view_detail_contract():
        if not cfg.projects: return "[dim]No projects[/dim]"
        p = cfg.projects[min(sel,len(cfg.projects)-1)]
        from backend.core.contract import ContractManager
        c = ContractManager.load(Path(p.workspace_path))
        if not c: return "[dim]No contract[/dim]"
        lines = [f"Tech: {', '.join(c.tech_stack) if c.tech_stack else '(none)'}"]
        lines.append(f"Features ({len(c.decided_features)}):")
        for i,f in enumerate(c.decided_features[:8]):
            marker = "▶" if i==detail_sel else " "
            loc = f" → {f.location}" if f.location else ""
            lines.append(f" {marker} [{f.confirmed_count}x] {f.name}{loc}")
        if len(c.decided_features) > 8:
            lines.append(f" [dim]... and {len(c.decided_features)-8} more[/dim]")
        lines.append(f"\nConstraints ({len(c.architecture_constraints)}):")
        for i,ct in enumerate(c.architecture_constraints[:5]):
            marker = "▶" if i+len(c.decided_features)==detail_sel else " "
            lines.append(f" {marker} [red]-[/red] {ct}")
        return "\n".join(lines)

    def _view_detail_events():
        if not cfg.projects: return "[dim]No projects[/dim]"
        p = cfg.projects[min(sel,len(cfg.projects)-1)]
        events = _load_history(p.workspace_path)
        if not events: return "[dim]No events[/dim]"
        lines = []
        for i,e in enumerate(reversed(events[-15:])):
            marker = "▶" if i==detail_sel else " "
            op = e.get("operation","")
            if op.startswith("governance_"): op=op[11:]
            ts = e.get("timestamp","")[:19]
            lines.append(f" {marker} [dim]{ts}[/dim] {op:25s} {e.get('status','')}")
        return "\n".join(lines)

    def _build():
        nonlocal cmd_result
        l = Layout(); l.split_column(Layout(name="top"),Layout(name="bottom",size=3))

        if detail:
            title = f"[cyan]{cfg.projects[min(sel,len(cfg.projects)-1)].name if cfg.projects else '?'}[/cyan] {_view_detail_tabs()}"
            if detail_tab == 0:
                body = _view_lessons()
            elif detail_tab == 1:
                body = _view_detail_contract()
            else:
                body = _view_detail_events()
            l["top"].update(Panel(body,title=title,border_style="cyan"))
        else:
            table = _view_overview()
            l["top"].update(Panel(table,title="Gitgo Monitor [dim](↑↓ select  Enter detail  :cmd  h help  q quit)[/dim]",border_style="blue"))

        # 指令结果——始终显示在底部
        prompt = f" > [bold green]{cmd_buf}[/bold green]_" if cmd_buf is not None else " > [dim]:help 查看指令[/dim]_"
        if cmd_result:
            prompt = f"[yellow]{cmd_result}[/yellow]\n" + prompt
            cmd_result = ""
        l["bottom"].update(Panel(prompt,title="Command",border_style="green",padding=(0,1)))
        return l

    # ── Keyboard handler ────────────────────────────────────
    def _handle_key(key):
        nonlocal sel, detail, detail_tab, detail_sel, cmd_buf, cmd_result, cmd_hist_idx, dirty

        if key is None: return

        # Command mode
        if cmd_buf is not None:
            if key == 'enter':
                if cmd_buf.strip():
                    cmd_history.append(cmd_buf)
                    cmd_hist_idx = len(cmd_history)
                    cmd_result = _handle_cmd(cfg, cmd_buf)
                cmd_buf = None; dirty = True
            elif key == 'esc': cmd_buf = None; dirty = True
            elif key == 'backspace': cmd_buf = cmd_buf[:-1] if cmd_buf else ""; dirty = True
            elif key == 'up' and cmd_history:
                if cmd_hist_idx > 0: cmd_hist_idx -= 1; cmd_buf = cmd_history[cmd_hist_idx]; dirty = True
            elif key == 'down' and cmd_history:
                if cmd_hist_idx < len(cmd_history)-1: cmd_hist_idx += 1; cmd_buf = cmd_history[cmd_hist_idx]; dirty = True
            elif key == 'tab': pass  # ignore tab in cmd
            elif isinstance(key, str) and len(key) == 1: cmd_buf = (cmd_buf or "") + key; dirty = True
            return

        # Navigation mode
        if key == 'q': raise KeyboardInterrupt
        elif key == 'esc':
            if detail: detail = False; dirty = True
        elif key == ':': cmd_buf = ""; dirty = True

        elif detail:
            if key == 'left': detail_tab = max(0, detail_tab-1); detail_sel = 0; dirty = True
            elif key == 'right': detail_tab = min(2, detail_tab+1); detail_sel = 0; dirty = True
            elif key == 'up' and detail_sel > 0: detail_sel -= 1; dirty = True
            elif key == 'down': detail_sel += 1; dirty = True
            elif key == 'enter':
                # Future: open single item detail
                pass
        else:
            if key == 'h': cmd_result = _handle_cmd(cfg, "help"); dirty = True
            elif key == 'enter':
                if cfg.projects: detail = True; detail_tab = 0; detail_sel = 0; dirty = True
            elif key == 'up' and sel > 0: sel -= 1; dirty = True
            elif key == 'down' and sel < len(cfg.projects)-1: sel += 1; dirty = True

    # ── Main loop ────────────────────────────────────────────
    _trace = []; _t0 = time.time()
    def _t(msg): _trace.append(f"{(time.time()-_t0):.4f} {msg}")
    try:
        with Live(_build(), console=console, screen=True) as live:
            last_redraw = 0.0
            while True:
                now = time.time()

                if not detail and now - _cache_time >= refresh:
                    _refresh_cache()
                need_render = dirty or (not detail and now - last_redraw >= refresh)
                if need_render:
                    _t(f"RENDER detail={detail} dirty={bool(dirty)}")
                    live.update(_build())
                    last_redraw = now; dirty = False

                if _kbhit():
                    key = _getch()
                    if key is None: time.sleep(0.02); continue
                    while _kbhit(): _getch()
                    _t(f"KEY {key}")
                    try: _handle_key(key)
                    except KeyboardInterrupt: break
                else:
                    time.sleep(0.05)
    except KeyboardInterrupt: pass
    finally:
        import tempfile
        fp = Path(tempfile.gettempdir()) / "gitgo_dashboard_trace.log"
        fp.write_text(f"TRACE entries={len(_trace)}\n" + "\n".join(_trace), encoding="utf-8")
        import sys as _sys
        print(f"\n\n[DASHBOARD] trace -> {fp}", file=_sys.stderr)
