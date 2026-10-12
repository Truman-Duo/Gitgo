import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {selectRuntime} from './runtime.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
function option(name, fallback) {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  if (!args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`${name} requires a value`);
  return args.splice(index, 2)[1];
}
try {
  const preference = option('--runtime', process.env.GITGO_DASHBOARD_RUNTIME || 'auto');
  const entry = option('--entry', 'cli');
  if (!['cli', 'trace'].includes(entry)) throw new Error('--entry must be cli or trace');
  const {selected, probes} = selectRuntime(preference);
  const diagnostic = {
    selected, probes, stdinTTY: !!process.stdin.isTTY, stdoutTTY: !!process.stdout.isTTY,
    terminal: process.env.TERM || '',
    keyboard: 'Ctrl/Cmd delivery depends on the terminal; the probe cannot detect intercepted shortcuts',
  };
  if (args.includes('--runtime-diagnostics')) {
    console.log(JSON.stringify(diagnostic, null, 2));
  } else {
    const bundle = resolve(root, 'dist', `${entry}.js`);
    if (!existsSync(bundle)) throw new Error('Dashboard bundle missing. Run bun run build first (build tools require Bun).');
    console.error(`[gitgo-dashboard] ${selected.runtime} ${selected.version} (${selected.executable})`);
    for (const probe of probes.filter(p => !p.available)) console.error(`[gitgo-dashboard] ${probe.runtime} unavailable: ${probe.reason}`);
    const child = spawn(selected.executable, [bundle, ...args], {stdio: 'inherit', env: process.env});
    const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
    const handlers = signals.map(signal => {
      const handler = () => { if (!child.killed) child.kill(signal); };
      process.on(signal, handler);
      return [signal, handler];
    });
    child.on('error', error => { console.error(error.message); process.exitCode = 1; });
    child.on('exit', (code, signal) => {
      for (const [name, handler] of handlers) process.removeListener(name, handler);
      process.exitCode = code ?? ({SIGINT: 130, SIGTERM: 143, SIGHUP: 129}[signal] || 1);
    });
  }
} catch (error) {
  console.error(`[gitgo-dashboard] ${error.message}`);
  process.exitCode = 1;
}
