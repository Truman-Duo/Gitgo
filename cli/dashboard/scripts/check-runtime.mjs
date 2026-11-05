import {assertRuntimeSecurity} from './runtime.mjs';
try {
  assertRuntimeSecurity();
  console.log('Runtime security check:', process.versions.bun ? 'Bun ' + process.versions.bun : 'Node ' + process.versions.node);
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
