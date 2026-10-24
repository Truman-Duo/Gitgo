import { describe, expect, test } from 'bun:test'
import { getCommands, getKeybindings, REGISTRY, type CommandDef } from './keybindings.js'
import { executeCommand } from './commands.js'

function names(scene: Parameters<typeof getKeybindings>[0]): string[] {
  return getKeybindings(scene).map((command) => command.name)
}

describe('scene-owned command registry', () => {
  test('/update belongs only to the project-list homepage, including manual dispatch', async () => {
    const ctx: any = {client: {callTool: async () => {throw new Error('must not call backend');}},
      projects: [], sel: 0, activeProject: null, refresh: async () => {}};
    expect(getCommands('projects').map(item => item.label)).toContain('/update');
    expect(getCommands('projects', '/upd', 'zh')[0]?.description).toBe('软件更新');
    expect((await executeCommand('/update', {...ctx, scene: 'projects'})).showPanel?.overlay).toBe('updatePanel');
    expect((await executeCommand('/update apply', {...ctx, scene: 'projects'})).showPanel).toBeUndefined();
    for (const scene of ['workspace', 'agent_detail', 'process_list'] as const) {
      expect(getCommands(scene).map(item => item.label)).not.toContain('/update');
      expect((await executeCommand('/update', {...ctx, scene})).showPanel).toBeUndefined();
    }
  });
  test('ProcessList exposes only its actions and scene help', () => {
    expect(getCommands('process_list').map(item => item.label).sort()).toEqual([
      '/archive', '/create', '/detail', '/help', '/rename',
    ]);
  });

  test('manual B creation is an explicit A admission, not a fork acknowledgement', async () => {
    const submitted:string[]=[];
    const base:any={client:{callTool:async()=>({})},projects:[{name:'alpha'}],sel:0,
      activeProject:'alpha',scene:'process_list',refresh:async()=>{}};
    const outcome=await executeCommand('/create build an accessible report', {
      ...base,createB:(text:string)=>{submitted.push(text);return true;},
    });
    expect(submitted).toEqual(['build an accessible report']);
    expect(outcome.navigateTo).toBe('workspace');
    const rejected=await executeCommand('/create do another task', {...base,createB:()=>false});
    expect(rejected.resultText).toContain('GITGO-E3107');
  });

  test('process edits target the selected B of the active project, not A or the project-list cursor', async () => {
    const calls: any[] = [];
    const ctx: any = {client: {callTool: async (...args: any[]) => calls.push(args)},
      projects: [{name: 'other'}], sel: 0, activeProject: 'current', scene: 'process_list',
      activeProcessId: 'a', selectedProcessId: 'b', refresh: async () => {}, refreshProcesses: async () => {}};
    await executeCommand('/rename HTML author', ctx);
    await executeCommand('/archive', ctx);
    expect(calls).toEqual([
      ['process.rename', {project: 'current', process_id: 'b', display_name: 'HTML author'}],
      ['process.archive', {project: 'current', process_id: 'b', archived: true}],
    ]);
    expect(getCommands('process_list', '/rename')[0].inputMode).toBe('fill');
  });
  test('does not leak project commands into A/B workspaces', () => {
    expect(names('projects')).toContain('projects.config')
    expect(names('projects')).not.toContain('workspace.runtime')
    expect(names('workspace')).toContain('workspace.runtime')
    expect(names('workspace')).toContain('projects.config')
    expect(names('agent_detail')).toContain('workspace.runtime')
    expect(names('agent_detail')).toContain('projects.config')
    expect(names('projects')).toContain('projects.bin')
    for (const scene of ['workspace', 'agent_detail', 'process_list'] as const) {
      expect(names(scene)).not.toContain('projects.bin')
    }
  })

  test('resolves only the selected scene command subtree', () => {
    expect(getCommands('projects', '/runtime ')).toEqual([])
    expect(getCommands('workspace', '/config ').map((item) => item.label)).toContain('general')
    expect(getCommands('workspace', '/runtime lesson ').map((item) => item.label)).toEqual([
      'list',
      'search',
      'verify',
    ])
    expect(getCommands('workspace', '/runtime recovery ').map((item) => item.label)).toEqual([
      'discard', 'resume', 'resume_verified',
    ])
  })

  test('exposes first-release runtime commands and localizes descriptions only', () => {
    expect(names('projects')).toContain('projects.stats_overview')
    expect(names('projects')).not.toContain('workspace.stats')
    expect(names('workspace')).toContain('workspace.stats')
    expect(names('workspace')).not.toContain('projects.stats_overview')
    const workspace = getCommands('workspace')
    expect(workspace.map((item) => item.label)).toContain('/compact')
    expect(workspace.map((item) => item.label)).toContain('/undo')
    expect(workspace.map((item) => item.label)).toContain('/btw')
    expect(workspace.find((item) => item.label === '/btw')?.inputMode).toBe('fill')
    expect(workspace.map((item) => item.label)).toContain('/stats')
    const chinese = getCommands('workspace', '/comp', 'zh')[0]
    expect(chinese.label).toBe('/compact')
    expect(chinese.description).toBe('压缩上下文')
  })

  test('previews /undo and leaves mutation to the confirmation panel', async () => {
    const calls:any[]=[]
    const ctx:any={client:{callTool:async (...args:any[])=>{calls.push(args);return {
      checkpoint_id:'cp-1',process_id:'root-1',warning:'session only',turn_preview:'last turn',
    }}},projects:[{name:'demo'}],sel:0,activeProject:'demo',scene:'workspace',
      activeProcessId:'root-1',refresh:async()=>{}}
    const outcome=await executeCommand('/rewind',ctx)
    expect(calls).toEqual([['runtime.undo.preview',{project:'demo',process_id:'root-1'},35]])
    expect(outcome.showPanel?.overlay).toBe('undoPanel')
    expect(outcome.showPanel?.props?.preview.checkpoint_id).toBe('cp-1')
  })

  test('opens BTW immediately so the panel can stream progress and errors', async () => {
    let calls = 0
    const ctx: any = {
      client: { callTool: async () => { calls += 1; throw new Error('provider unavailable') } },
      projects: [{ name: 'demo' }], sel: 0, activeProject: 'demo',
      refresh: async () => {}, scene: 'workspace', activeProcessId: 'root-1',
    }
    const outcome = await executeCommand('/btw hello', ctx)
    expect(calls).toBe(0)
    expect(outcome.showPanel?.overlay).toBe('btwPanel')
    expect(outcome.showPanel?.props?.question).toBe('hello')
  })

  test('attaches /btw to the selected process without mutating it', async () => {
    let request: any[] = []
    const ctx: any = {
      client: { callTool: async (...args: any[]) => {
        request = args
        return { answer: 'side answer', isolated: true, sidecar_id: 'side-1' }
      } },
      projects: [{ name: 'demo' }], sel: 0, activeProject: 'demo',
      refresh: async () => {}, scene: 'workspace', activeProcessId: 'root-1',
    }
    const outcome = await executeCommand('/btw what changed?', ctx)
    expect(request).toEqual([])
    expect(outcome.showPanel?.props?.processId).toBe('root-1')
    expect(outcome.showPanel?.props?.sidecarId).toBeTruthy()
  })

  test('opens manual compaction before starting the provider-backed operation', async () => {
    let request: any[] = []
    const ctx: any = {
      client: { callTool: async (...args: any[]) => {
        request = args
        return { status: 'completed', changed: true }
      } },
      projects: [{ name: 'demo' }], sel: 0, activeProject: 'demo',
      refresh: async () => {}, scene: 'workspace', activeProcessId: 'root-1',
    }
    const outcome = await executeCommand('/compact', ctx)
    expect(request).toEqual([])
    expect(outcome.resultText).toBe('')
    expect(outcome.showPanel?.overlay).toBe('compactPanel')
    expect(outcome.showPanel?.props).toEqual({
      project: 'demo', processId: 'root-1',
      result: {status: 'starting', attempt: 0},
    })
  })

  test('distinguishes queued compaction from a completed cold-session compaction', async () => {
    const ctx: any = {
      client: { callTool: async () => ({ status: 'queued' }) },
      projects: [{ name: 'demo' }], sel: 0, activeProject: 'demo',
      refresh: async () => {}, scene: 'workspace', activeProcessId: 'root-1',
    }
    const outcome = await executeCommand('/compact', ctx)
    expect(outcome.resultText).toBe('')
    expect(outcome.showPanel?.props?.result?.status).toBe('starting')
  })

  test('routes compaction failures and explicit approvals to the maintenance panel', async () => {
    for (const status of ['failed', 'awaiting_user']) {
      const result = { status, attempt: 1, retry_compaction: true }
      const ctx: any = {
        client: { callTool: async () => result },
        projects: [{ name: 'demo' }], sel: 0, activeProject: 'demo',
        refresh: async () => {}, scene: 'agent_detail', activeProcessId: 'b-1',
      }
      const outcome = await executeCommand('/compact', ctx)
      expect(outcome.showPanel?.overlay).toBe('compactPanel')
      expect(outcome.showPanel?.props?.result).toEqual({status: 'starting', attempt: 0})
      expect(outcome.showPanel?.props?.processId).toBe('b-1')
      expect(outcome.resultText).not.toBe('Context compacted')
    }
  })
})


test("every command and descendant has explicit translations; suggestions and Help agree",()=>{
 function visit(defs:CommandDef[]){
  for(const def of defs){
   expect(def.title.en.trim().length).toBeGreaterThan(0);
   expect(def.title.zh).toMatch(/[\u3400-\u9fff]/);
   visit(def.children||[]);
  }
 }
 visit(REGISTRY);
 for(const scene of ["projects","workspace","process_list","agent_detail"] as const){
  const menu=getCommands(scene,undefined,"zh"),help=getKeybindings(scene,"zh");
  expect(menu.map(x=>x.label).sort()).toEqual(help.map(x=>"/"+x.slashName).sort());
  for(const item of menu)expect(item.description).toBe(help.find(d=>"/"+d.slashName===item.label)?.title ?? "MISSING_TRANSLATION");
 }
 expect(getCommands("projects","/pub","zh")).toEqual([{label:"/publish",description:"发布管理",inputMode:undefined}]);
 expect(getCommands("workspace","/publish ","zh").map(x=>x.description)).toEqual(["正式提交","试验变更"]);
 expect(getCommands("workspace","/runtime lesson ","zh").every(x=>/[\u3400-\u9fff]/.test(x.description||""))).toBe(true);
 expect(getCommands("projects","/publish","en")[0]?.description).toBe("Publish");
});


test("/detail binds to the selected or current subprocess without a backend operation",async()=>{
 const base:any={client:{callTool:async()=>{throw Error("details must be read-only");}},projects:[{name:"other"}],
  sel:0,activeProject:"current",refresh:async()=>{},activeProcessId:"current-child",selectedProcessId:"selected-child"};
 expect((await executeCommand("/detail",{...base,scene:"process_list"})).showPanel)
  .toEqual({overlay:"processDetail",props:{project:"current",processId:"selected-child"}});
 expect((await executeCommand("/detail",{...base,scene:"agent_detail"})).showPanel)
  .toEqual({overlay:"processDetail",props:{project:"current",processId:"current-child"}});
 for(const scene of ["workspace","projects"] as const){
  expect(getCommands(scene).some(c=>c.label==="/detail")).toBe(false);
  expect((await executeCommand("/detail",{...base,scene})).showPanel).toBeUndefined();
 }
 expect((await executeCommand("/detail",{...base,scene:"process_list",selectedProcessId:undefined})).showPanel?.props?.processId).toBe("");
});
