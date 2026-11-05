import {spawnSync} from 'node:child_process';

// Reviewed security floors; unreviewed Node release lines are not selected.
export const minimumVersions = {bun: [1, 4, 4], node: [22, 23, 4]};
export const nodeSecurityFloors = {22: [22, 23, 4], 24: [24, 22, 1], 26: [26, 12, 1]};
export function runtimeRequirement(runtime) {
  return runtime === 'node'
    ? 'Node 22 >=22.23.4, 24 >=24.22.1, or 26 >=26.12.1 (stable)'
    : 'Bun >=1.4.4 (stable)';
}
export function supportedVersion(runtime, version) {
  if (!/^v?\d+\.\d+\.\d+$/.test(String(version))) return false;
  const parts = String(version).replace(/^v/, '').split('.').map(Number);
  const minimum = runtime === 'node' ? nodeSecurityFloors[parts[0]] : minimumVersions[runtime];
  if (!minimum) return false;
  for (let i = 0; i < 3; i++) {
    if (parts[i] !== minimum[i]) return parts[i] > minimum[i];
  }
  return true;
}

export function assertRuntimeSecurity(versions = process.versions) {
  const runtime = versions.bun ? 'bun' : 'node';
  if (!supportedVersion(runtime, versions.bun || versions.node)) {
    throw new Error('Unsupported runtime: requires ' + runtimeRequirement(runtime));
  }
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
      throw new Error(`requires ${runtimeRequirement(runtime)} with raw-mode and UTF-8 support`);
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
