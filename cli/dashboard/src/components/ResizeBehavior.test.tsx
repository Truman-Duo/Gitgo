import React, {createRef} from 'react';
import {expect, test} from 'bun:test';
import {PassThrough} from 'node:stream';
import {Box, Text, ScrollBox, renderSync, useTerminalSize, useVirtualScroll} from '@anthropic/ink';
import type {ScrollBoxHandle} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {InputProvider} from '../input/runtime.js';
import {usePanelSize} from '../theme/usePanelSize.js';
import {Overview} from './Overview.js';
import {ProcessList} from './ProcessList.js';
import {DialogSelect} from './DialogSelect.js';
import {ChatPanel} from './ChatPanel.js';
import {AgentDetail} from './AgentDetail.js';
import {RuntimeContextPanel} from './RuntimeContextPanel.js';
import {DiffView} from './DiffView.js';

const settle = () => new Promise(resolve => setTimeout(resolve, 100));
function text(node: any): string {
  return node.nodeName === '#text' ? String(node.nodeValue || '') : (node.childNodes || []).map(text).join('');
}
function nodes(node: any): any[] { return [node, ...(node.childNodes || []).flatMap(nodes)]; }
function terminal(columns = 100, rows = 18) {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = true; stdout.columns = columns; stdout.rows = rows;
  const stderr: any = new PassThrough(); stdout.on('data', () => {}); stderr.on('data', () => {});
  return {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false};
}
function dispose(root: ReturnType<typeof renderSync>, tty: ReturnType<typeof terminal>) {
  root.unmount(); root.cleanup(); tty.stdin.destroy(); tty.stdout.destroy(); tty.stderr.destroy();
}

for (const count of [24, 400]) {
  test(`within-message reading position survives reverse resize (${count} items)`, async () => {
    const tty = terminal(), scroll = createRef<ScrollBoxHandle>();
    const keys = Array.from({length: count}, (_, i) => `row-${i}`);
    const anchor = Math.floor(count / 2);
    function Fixture() {
      const {columns} = useTerminalSize();
      const height = columns < 80 ? 8 : 4;
      const virtual = useVirtualScroll({scrollRef: scroll, itemKeys: keys, estimateSize: height, layoutKey: columns});
      return <Box height={18} width="100%" flexDirection="column">
        <ScrollBox ref={scroll} flexGrow={1} flexDirection="column" stickyScroll={false}>
          <Box height={virtual.before} flexShrink={0}/>
          {keys.slice(virtual.start, virtual.end).map(key =>
            <Box key={key} ref={virtual.itemRef(key)} height={height} flexShrink={0}><Text>{key}</Text></Box>)}
          <Box height={virtual.after} flexShrink={0}/>
        </ScrollBox>
      </Box>;
    }
    const root = renderSync(<Fixture/>, tty);
    try {
      await settle(); scroll.current!.scrollTo(anchor * 4 + 3); await settle();
      tty.stdout.columns = 50; tty.stdout.emit('resize'); await settle();
      expect(Math.floor(scroll.current!.getScrollTop() / 8)).toBe(anchor);
      scroll.current!.scrollTo(anchor * 8 + 7); await settle();
      for (const columns of [100, 50, 100]) {
        tty.stdout.columns = columns; tty.stdout.emit('resize'); await settle();
        expect(Math.floor(scroll.current!.getScrollTop() / (columns < 80 ? 8 : 4))).toBe(anchor);
        expect(scroll.current!.isSticky()).toBe(false);
      }
    } finally { dispose(root, tty); }
  });
}

test('ordinary scroll panels preserve the same block when its wrapped height shrinks', async () => {
  const tty = terminal(), scroll = createRef<ScrollBoxHandle>();
  function Fixture() {
    const {columns} = useTerminalSize(); const height = columns < 80 ? 8 : 4;
    return <Box height={18} width="100%" flexDirection="column">
      <ScrollBox ref={scroll} flexGrow={1} flexDirection="column" stickyScroll={false}>
        {Array.from({length: 40}, (_, i) => <Box key={i} flexShrink={0}>
          <Text>{Array.from({length: height}, (_, line) => `row-${i} line-${line}`).join('\n')}</Text>
        </Box>)}
      </ScrollBox>
    </Box>;
  }
  const root = renderSync(<Fixture/>, tty);
  try {
    await settle(); scroll.current!.scrollTo(10 * 4 + 3); await settle();
    tty.stdout.columns = 50; tty.stdout.emit('resize'); await settle();
    scroll.current!.scrollTo(10 * 8 + 7); await settle();
    tty.stdout.columns = 100; tty.stdout.emit('resize'); await settle();
    expect(Math.floor(scroll.current!.getScrollTop() / 4)).toBe(10);
    expect(scroll.current!.isSticky()).toBe(false);
  } finally { dispose(root, tty); }
});

test('panel sizes respect a narrow and short physical viewport', async () => {
  const tty = terminal(32, 8); let size: ReturnType<typeof usePanelSize>;
  function Fixture() {
    size = usePanelSize({minWidth: 60, minHeight: 30, widthOffset: 4, headerRows: 5, footerRows: 4});
    return <Text>{size.w}</Text>;
  }
  const root = renderSync(<Fixture/>, tty);
  try {
    await settle(); expect(size!.w).toBe(28); expect(size!.h).toBe(8);
    expect(size!.contentH).toBe(1); expect(size!.maxVisibleItems(10)).toBe(1);
  } finally { dispose(root, tty); }
});

function selectedRowIsVisible(tty: ReturnType<typeof terminal>, label: string) {
  const all = nodes((instances.get(tty.stdout) as any).rootNode);
  const viewport = all.find(node => node.scrollViewportHeight !== undefined);
  expect(viewport).toBeDefined();
  const row = all.filter(node => node.nodeName === 'ink-box' && text(node).includes(label)).at(-1);
  expect(row).toBeDefined();
  let top = 0, node = row;
  while (node && node !== viewport.childNodes[0]) { top += node.yogaNode?.getComputedTop() ?? 0; node = node.parentNode; }
  expect(node).toBe(viewport.childNodes[0]);
  expect(top).toBeGreaterThanOrEqual(viewport.scrollTop);
  expect(top + Math.min(row.yogaNode.getComputedHeight(), viewport.scrollViewportHeight))
    .toBeLessThanOrEqual(viewport.scrollTop + viewport.scrollViewportHeight);
}

test('project selection remains visible below a full screen and after resize', async () => {
  const tty = terminal(60, 14);
  const projects: any[] = Array.from({length: 40}, (_, i) => ({name: `PROJECT-${i}`, workspace: `/work/project-${i}`,
    daemonOnline: false, stateAvailable: true, activeProcessCount: 0, waitingProcessCount: 0}));
  function Fixture({selected}: {selected: number}) {
    const {columns, rows} = useTerminalSize();
    return <Box height={rows} width="100%" flexDirection="column">
      <Overview projects={projects} sel={selected} mode="NORMAL" cols={columns} listActive/>
    </Box>;
  }
  const root = renderSync(<Fixture selected={0}/>, tty);
  try {
    await settle(); root.rerender(<Fixture selected={39}/>); await settle();
    selectedRowIsVisible(tty, 'PROJECT-39');
    tty.stdout.columns = 34; tty.stdout.rows = 10; tty.stdout.emit('resize'); await settle();
    selectedRowIsVisible(tty, 'PROJECT-39');
    root.rerender(<Fixture selected={0}/>); await settle(); selectedRowIsVisible(tty, 'PROJECT-0');
  } finally { dispose(root, tty); }
});

test('subprocess selection scrolls into view without displacing the footer', async () => {
  const tty = terminal(65, 18);
  const processes = Object.fromEntries(Array.from({length: 35}, (_, i) => [`id-${i}`, {
    process_id: `id-${i}`, display_name: `WORKER-${i}`, actor_kind: 'worker', status: 'running',
    created_at: String(i).padStart(3, '0'), dependency_ids: [], child_ids: [], steps_used: 1, max_steps: 10, ring_level: 0,
  }]));
  const loopData: any = {processes, recoveryAvailable: []};
  function Fixture({selected}: {selected: number}) {
    const {columns, rows} = useTerminalSize();
    return <Box height={rows} width="100%" flexDirection="column">
      <InputProvider>
        <ProcessList project="fixture" loopData={loopData} cols={columns} selIdx={selected}/>
      </InputProvider>
    </Box>;
  }
  const root = renderSync(<Fixture selected={0}/>, tty);
  try {
    await settle(); root.rerender(<Fixture selected={34}/>); await settle();
    selectedRowIsVisible(tty, 'id-34');
    tty.stdout.columns = 36; tty.stdout.rows = 14; tty.stdout.emit('resize'); await settle();
    selectedRowIsVisible(tty, 'id-34');
    expect(text((instances.get(tty.stdout) as any).rootNode)).toContain('detail');
  } finally { dispose(root, tty); }
});

test('command selector displays and confirms the selected item past its first page', async () => {
  const tty = terminal(50, 20); let chosen = '';
  const events = Array.from({length: 25}, (_, i) => ({afterMs: 100 + i * 10, input: '', key: {downArrow: true}}));
  const root = renderSync(<Box width="100%" height={20}><InputProvider verificationInput={events}>
    <DialogSelect items={Array.from({length: 40}, (_, i) => ({id: `id-${i}`, title: `CHOICE-${i}`, category: 'Commands'}))}
      height={20} onSelect={id => { chosen = id; }} onDismiss={() => {}}/>
  </InputProvider></Box>, tty);
  try {
    await new Promise(resolve => setTimeout(resolve, 600));
    expect(text((instances.get(tty.stdout) as any).rootNode)).toContain('CHOICE-25');
    tty.stdin.write('\r'); await settle(); expect(chosen).toBe('id-25');
  } finally { dispose(root, tty); }
});

for (const subprocess of [false, true]) {
  test(`${subprocess ? 'subprocess' : 'main'} conversation fits a narrow physical pane`, async () => {
    const tty = terminal(32, 18), chat = {current: null};
    const props = {messages: [], streaming: null, cols: 32, scrollChatRef: chat};
    const root = renderSync(<Box width="100%" height={18}><InputProvider>
      {subprocess ? <AgentDetail {...props} rows={18} toolEvents={[]} process={{
        process_id: 'fixture', display_name: 'Fixture', steps_used: 0, max_steps: 10,
      } as any}/> : <ChatPanel {...props}/>}
    </InputProvider></Box>, tty);
    try {
      await settle();
      const all = nodes((instances.get(tty.stdout) as any).rootNode);
      const pane = all.find(node => node.nodeName === 'ink-box' && node.style?.paddingLeft === 1);
      expect(pane).toBeDefined(); expect(pane.yogaNode.getComputedWidth()).toBeLessThanOrEqual(32);
    } finally { dispose(root, tty); }
  });
}

test('context tabs keep the active process visible when there are many processes', async () => {
  const tty = terminal(34, 20);
  const processes: any = Object.fromEntries(Array.from({length: 30}, (_, i) => [`id-${i}`, {
    process_id: `id-${i}`, display_name: `WORKER-${i}`, actor_kind: 'worker',
    created_at: String(i).padStart(3, '0'), model_id: `MODEL-${i}`,
  }]));
  // Key repeats can arrive in one render batch on a busy terminal.
  const events = Array.from({length: 25}, () => ({afterMs: 100, input: '', key: {rightArrow: true}}));
  const root = renderSync(<Box width="100%" height={20}><InputProvider verificationInput={events}>
    <RuntimeContextPanel project="fixture" processes={processes} onDismiss={() => {}}/>
  </InputProvider></Box>, tty);
  try {
    await new Promise(resolve => setTimeout(resolve, 600));
    const all = nodes((instances.get(tty.stdout) as any).rootNode);
    const header = all.find(node => node.nodeName === 'ink-box' && node.style?.overflowX === 'hidden'
      && text(node).includes('WORKER-25'));
    expect(header).toBeDefined(); expect(header.yogaNode.getComputedWidth()).toBeLessThanOrEqual(32);
    expect(text((instances.get(tty.stdout) as any).rootNode)).toContain('MODEL-25');
  } finally { dispose(root, tty); }
});

test('narrow unified diff rows fit the allocated content width', async () => {
  const tty = terminal(24, 18);
  const root = renderSync(<Box width="100%" height={18} flexDirection="column">
    <DiffView width={24} files={[{file: 'fixture.ts', status: 'modified', additions: 1, deletions: 1, hunks: [{
      oldStart: 100, oldLines: 1, newStart: 100, newLines: 1,
      lines: [{type: 'remove', text: 'before'.repeat(10)}, {type: 'add', text: 'after'.repeat(10)}],
    }]}]}/>
  </Box>, tty);
  try {
    await settle();
    const all = nodes((instances.get(tty.stdout) as any).rootNode);
    for (const node of all.filter(node => node.nodeName === 'ink-box')) {
      expect(node.yogaNode.getComputedWidth()).toBeLessThanOrEqual(24);
    }
  } finally { dispose(root, tty); }
});
