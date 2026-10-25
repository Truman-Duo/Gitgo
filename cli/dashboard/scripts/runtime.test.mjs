import {test} from 'node:test';
import assert from 'node:assert/strict';
import {selectRuntime, supportedVersion, probeRuntime, assertRuntimeSecurity} from './runtime.mjs';

test('minimum versions, malformed and prerelease versions', () => {
  assert.equal(supportedVersion('node', 'v22.23.4'), true);
  assert.equal(supportedVersion('node', '21.9.0'), false);
  assert.equal(supportedVersion('bun', '1.4.4'), true);
  assert.equal(supportedVersion('bun', '1.2.99'), false);
  assert.equal(supportedVersion('node', '22.0.0-rc1'), false);
  assert.equal(supportedVersion('node', 'garbage'), false);
});

test('each Node line rejects vulnerable versions and unreviewed lines', () => {
  for (const version of ['22.0.0', '22.23.3', '24.15.0', '24.22.0', '26.12.0', '23.99.0', '25.99.0', '27.0.0', '24.22.1.1', '24.22.1-rc.1']) {
    assert.equal(supportedVersion('node', version), false, version);
  }
  for (const version of ['22.23.4', '22.24.0', '24.22.1', '24.23.0', '26.12.1']) assert.equal(supportedVersion('node', version), true, version);
  for (const version of ['1.3.14', '1.4.2', '1.4.3', '1.4.4-canary']) assert.equal(supportedVersion('bun', version), false);
  assert.throws(() => assertRuntimeSecurity({node: '24.15.0'}), /security|requires/i);
  assert.throws(() => assertRuntimeSecurity({bun: '1.4.2', node: '24.22.1'}), /Bun/);
  assert.doesNotThrow(() => assertRuntimeSecurity({bun: '1.4.4', node: '24.15.0'}));
});
test('Bun is preferred; failed probes fall back to Node; explicit overrides fail closed', () => {
  const probe = (runtime, executable) => ({runtime, executable, available: runtime === 'node', reason: 'probe failed'});
  assert.equal(selectRuntime('auto', {}, probe).selected.runtime, 'node');
  assert.equal(selectRuntime('auto', {}, (runtime, executable) => ({runtime, executable, available: true})).selected.runtime, 'bun');
  assert.equal(selectRuntime('node', {GITGO_NODE: '/custom/node'}, probe).selected.executable, '/custom/node');
  assert.throws(() => selectRuntime('bun', {}, probe), /probe failed/);
  assert.throws(() => selectRuntime('other'), /Invalid runtime/);
});
test('real executable probes verify identity, raw mode and split UTF-8', {timeout: 20000}, () => {
  const node = probeRuntime('node', process.env.GITGO_NODE || 'node');
  const bun = probeRuntime('bun', process.env.GITGO_BUN || 'bun');
  assert.equal(node.available, true, JSON.stringify(node));
  assert.equal(bun.available, true, JSON.stringify(bun));
  assert.equal(probeRuntime('bun', process.env.GITGO_NODE || 'node').available, false);
  assert.equal(probeRuntime('node', '/nonexistent/gitgo-runtime').available, false);
});
