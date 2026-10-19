import {existsSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

/** Works for source, a shared JS bundle, and a native distribution. */
export function resolveProjectRoot(moduleUrl: string): string {
  if (process.env.GITGO_INSTALL_ROOT) return resolve(process.env.GITGO_INSTALL_ROOT);
  let directory = dirname(fileURLToPath(moduleUrl));
  for (;;) {
    if (existsSync(resolve(directory, 'product.json')) || existsSync(resolve(directory, 'backend/core/native_host_entry.py'))) return directory;
    const parent = dirname(directory);
    if (parent === directory) throw new Error('Gitgo root not found; set GITGO_INSTALL_ROOT');
    directory = parent;
  }
}
