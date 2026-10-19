// Diagnostic fixture for automated PTY tests; not the product acceptance UI.
import React, {useLayoutEffect} from 'react';
import {renderSync, Text, useInput, useTerminalSize} from '@anthropic/ink';
import {InputProvider, useManagedInput} from '../src/input/runtime.js';
const report = (event: unknown) => process.stderr.write(JSON.stringify(event) + '\n');
function Probe() {
  const {columns, rows} = useTerminalSize();
  useLayoutEffect(() => {report({size: [columns, rows]});}, [columns, rows]);
  useInput((input, key) => {if (key.super && input === 'c') report({cmdCopy: true});});
  useManagedInput((input, key) => report({input, ctrl: !!key.ctrl, shift: !!key.shift,
    enter: !!key.return, paste: !!key.paste}));
  return <Text>Gitgo terminal probe</Text>;
}
const renderer = renderSync(<InputProvider><Probe /></InputProvider>, {exitOnCtrlC: false, patchConsole: false});
process.once('SIGTERM', () => {renderer.unmount(); renderer.cleanup(); process.exit(143);});
setTimeout(() => report({ready: true}), 100);
