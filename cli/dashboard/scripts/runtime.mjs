import {spawnSync} from 'node:child_process';

export const minimumVersions = {bun: [1, 3, 0], node: [22, 0, 0]};
export function supportedVersion(runtime, version) {
  const parts = String(version).replace(/^v/, '').split('.').map(Number);
  if (parts.length < 3 || parts.slice(0, 3).some(n => !Number.isInteger(n))) return false;
  const minimum = minimumVersions[runtime];
  if (!minimum) return false;
  for (let i = 0; i < 3; i++) {
    if (parts[i] !== minimum[i]) return parts[i] > minimum[i];
  }
  return !String(version).includes('-');
}

// Probe the executable, not just its filename: overrides may point at another
// runtime, an old version, or an executable that fails to start on this host.
export function probeRuntime(runtime, executable) {
  const code = `const {StringDecoder}=require('node:string_decoder');
    const {ReadStream}=require('node:tty');
    const d=new StringDecoder('utf8');const b=Buffer.from('中文𠮷');
    const text=[...b].map(x=>d.write(Buffer.from([x]))).join('')+d.end();
    console.log(JSON.stringify({runtime:process.versions.bun?'bun':'node',
      version:process.versions.bun||process.versions.node,
      raw:typeof ReadStream.prototype.setRawMode==='function',unicode:text==='中文𠮷'}));`;
  const result = spawnSync(executable, ['-e', code], {encoding: 'utf8', timeout: 5000, windowsHide: true});
  try {
    if (result.error || result.status !== 0) throw result.error || new Error(result.stderr || `exit ${result.status}`);
    const data = JSON.parse(result.stdout.trim());
    if (data.runtime !== runtime || !supportedVersion(runtime, data.version) || !data.raw || !data.unicode) {
      throw new Error(`requires ${runtime} >= ${minimumVersions[runtime].join('.')} with raw-mode and UTF-8 support`);
    }
    return {runtime, executable, available: true, ...data};
  } catch (error) {
    return {runtime, executable, available: false, reason: error.message};
  }
}

export function selectRuntime(preference = 'auto', env = process.env, probe = probeRuntime) {
  if (!['auto', 'bun', 'node'].includes(preference)) throw new Error(`Invalid runtime: ${preference}; use auto, bun, or node`);
  const names = preference === 'auto' ? ['bun', 'node'] : [preference];
  const probes = names.map(name => probe(name, env[`GITGO_${name.toUpperCase()}`] || name));
  const selected = probes.find(item => item.available);
  if (!selected) throw new Error(probes.map(item => `${item.runtime}: ${item.reason}`).join('\n'));
  return {selected, probes};
}
