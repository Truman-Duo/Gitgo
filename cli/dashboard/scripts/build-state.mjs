import {createHash} from 'node:crypto';
import {existsSync, readdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';

const directories = ['src', 'vendor/ink/src', 'scripts'];
const files = ['package.json', 'bun.lock', 'tsconfig.json', 'vendor/ink/package.json', '../../backend/resources/keyboard/actions.json'];

/** Source-launch freshness only. Release executables do not run this launcher. */
export function sourceFingerprint(root) {
  const inputs = [];
  function visit(relative) {
    const path = join(root, relative);
    if (!existsSync(path)) return;
    for (const item of readdirSync(path, {withFileTypes: true})) {
      if (item.name === 'node_modules' || item.name === 'dist' || item.name.startsWith('.')) continue;
      const name = `${relative}/${item.name}`;
      if (item.isDirectory()) visit(name);
      else if (item.isFile() && !/\.(test|spec)\.[cm]?[jt]sx?$/.test(item.name)) inputs.push(name);
    }
  }
  directories.forEach(visit);
  files.filter(file => existsSync(join(root,file))).forEach(file => inputs.push(file));
  const hash = createHash('sha256');
  for (const file of inputs.sort()) hash.update(file).update('\0').update(readFileSync(join(root,file))).update('\0');
  return hash.digest('hex');
}

export function recordBuild(root) {
  writeFileSync(join(root,'dist/build-state.json'),JSON.stringify({version:1,fingerprint:sourceFingerprint(root)})+'\n');
}

export function currentBuild(root) {
  try {
    const manifest=JSON.parse(readFileSync(join(root,'dist/build-state.json'),'utf8'));
    return manifest.version === 1 && manifest.fingerprint === sourceFingerprint(root);
  } catch {return false;}
}
