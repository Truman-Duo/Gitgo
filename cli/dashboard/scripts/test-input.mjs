import {spawnSync} from 'node:child_process';
import {mkdirSync, readdirSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const output = resolve(root, '.runtime-tests');
mkdirSync(output, {recursive: true});
const entries = [
  'src/input/runtime.test.tsx', 'src/input/router.test.ts', 'src/input/keymap.test.ts',
  'src/input/pasteStore.test.ts', 'src/input/overlays/inlineContext.test.ts',
  'src/input/unicode.test.ts',
];
const build = spawnSync(process.env.GITGO_BUN || 'bun', ['build', ...entries,
  '--target=node', '--outdir', output], {cwd: root, stdio: 'inherit'});
if (build.error || build.status !== 0) throw build.error || new Error('Input test bundle failed');
const files = readdirSync(output, {recursive: true}).filter(name => name.endsWith('.test.js')).map(name => resolve(output, name));
for (const [runtime, flag] of [[process.env.GITGO_BUN || 'bun', 'test'], [process.env.GITGO_NODE || 'node', '--test']]) {
  console.log(`Input routing suite: ${runtime}`);
  const result = spawnSync(runtime, [flag, ...files], {cwd: root, stdio: 'inherit', timeout: 60000});
  if (result.error || result.status !== 0) throw result.error || new Error(`${runtime} input tests failed`);
}
