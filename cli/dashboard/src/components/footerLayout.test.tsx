import React from 'react';
import {expect, test} from 'bun:test';
import {PassThrough} from 'node:stream';
import {Box, Text, ScrollBox, renderSync} from '@anthropic/ink';
import instances from '../../vendor/ink/src/core/instances.js';
import {CommandBar} from './CommandBar.js';
import {useTextInput} from '../hooks/useTextInput.js';

// The real component + Yoga layout. This is NOT formal terminal visual acceptance.
test('command suggestions shrink the conversation and normal mode reclaims their exact height', async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 90; stdout.rows = 30;
  const stderr: any = new PassThrough(); stdout.on('data', () => {}); stderr.on('data', () => {});
  function Fixture({command, rows = 30, count = 70}: {command: boolean; rows?: number; count?: number}) {
    const text = useTextInput(''), cmd = useTextInput('');
    return <Box flexDirection="column" width={90} height={rows} flexShrink={0}>
      <Box flexGrow={1} flexBasis={0} minHeight={0} overflow="hidden">
        <ScrollBox flexDirection="column" flexGrow={1} stickyScroll>
          {Array.from({length: count}, (_, i) => <Text key={i}>Conversation line {i}</Text>)}
        </ScrollBox>
      </Box>
      <CommandBar width={90} mode={command ? 'COMMAND' : 'NORMAL'} scene="workspace"
        textInput={text} cmdInput={cmd} cmdResult="" statusText="test" suggestionIdx={0}
        suggestions={Array.from({length: 5}, (_, i) => ({label: `/command${i}`, description: 'example'}))}/>
    </Box>;
  }
  const root = renderSync(<Fixture command={false}/>, {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false});
  const settle = () => new Promise(resolve => setTimeout(resolve, 50));
  const findViewport = (node: any): any => node.scrollViewportHeight !== undefined ? node
    : (node.childNodes || []).map(findViewport).find(Boolean);
  const height = () => findViewport((instances.get(stdout) as any).rootNode).scrollViewportHeight;
  try {
    await settle(); const normal = height();
    for (let cycle = 0; cycle < 4; cycle++) {
      root.rerender(<Fixture command count={70 + cycle}/>); await settle();
      expect(height()).toBe(normal - 6);
      root.rerender(<Fixture command={false} count={70 + cycle}/>); await settle();
      const viewport = findViewport((instances.get(stdout) as any).rootNode);
      expect({painted: height(), layout: viewport.yogaNode.getComputedHeight(), parent: viewport.parentNode.yogaNode.getComputedHeight()})
        .toEqual({painted: normal, layout: normal, parent: normal});
      expect(viewport.scrollTop + height()).toBe(viewport.scrollHeight);
    }
    root.rerender(<Fixture command rows={24}/>); await settle();
    expect(height()).toBe(normal - 12);
    root.rerender(<Fixture command={false}/>); await settle();
    expect(height()).toBe(normal);
    expect((instances.get(stdout) as any).rootNode.yogaNode.getComputedHeight()).toBe(30);
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});

test('terminal reflow preserves a manually viewed conversation instead of snapping to bottom', async () => {
  const stdin: any = new PassThrough(); stdin.isTTY = true; stdin.setRawMode = () => stdin;
  stdin.ref = () => stdin; stdin.unref = () => stdin;
  const stdout: any = new PassThrough(); stdout.isTTY = false; stdout.columns = 100; stdout.rows = 18;
  const stderr: any = new PassThrough(); stdout.on('data', () => {}); stderr.on('data', () => {});
  const root = renderSync(
    <Box flexDirection="column" width="100%" height={18}>
      <ScrollBox flexGrow={1} flexDirection="column" stickyScroll={false}>
        {Array.from({length: 45}, (_, i) =>
          <Box key={i}><Text>{`Message ${i}: ` + 'wrapped content '.repeat(5)}</Text></Box>)}
      </ScrollBox>
    </Box>,
    {stdin, stdout, stderr, patchConsole: false, exitOnCtrlC: false},
  );
  const settle = () => new Promise(resolve => setTimeout(resolve, 60));
  const findViewport = (node: any): any => node.scrollViewportHeight !== undefined ? node
    : (node.childNodes || []).map(findViewport).find(Boolean);
  try {
    await settle();
    let viewport = findViewport((instances.get(stdout) as any).rootNode);
    viewport.scrollTop = 20;
    viewport.stickyScroll = false;
    stdout.columns = 55;
    stdout.emit('resize');
    await settle();
    viewport = findViewport((instances.get(stdout) as any).rootNode);
    expect(viewport.stickyScroll).toBe(false);
    expect(viewport.scrollTop + viewport.scrollViewportHeight).toBeLessThan(viewport.scrollHeight);
  } finally { root.unmount(); root.cleanup(); stdin.destroy(); stdout.destroy(); stderr.destroy(); }
});
