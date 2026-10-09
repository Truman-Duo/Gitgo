/** Optional owned launch lifetime; normal user launches have no session. */
import { randomUUID } from "node:crypto";
import { readFileSync, writeFileSync, realpathSync, renameSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

export function registerLaunchSession(environment = process.env): boolean {
  const path = environment.GITGO_LAUNCH_SESSION;
  if (!path) return false;
  try {
    const session = JSON.parse(readFileSync(path, "utf8"));
    const root = dirname(realpathSync(path));
    if (session.version !== 1 || session.token !== environment.GITGO_LAUNCH_SESSION_TOKEN ||
        !/^[a-f0-9-]{36}$/i.test(session.token) || realpathSync(session.root) !== root ||
        resolve(path) !== join(root, "session.json") ||
        realpathSync(process.execPath).toLowerCase() !== realpathSync(session.executable).toLowerCase()) {
      throw new Error("invalid session identity");
    }
    for (const [key, file] of Object.entries({GITGO_CONFIG_PATH: "config.json", GITGO_STATE_HOME: "state",
      GITGO_LLM_CONFIG_PATH: "llm_config.json", GITGO_LLM_SECRET_PATH: "provider_secrets.json"})) {
      if (resolve(environment[key] || "") !== join(root, file)) throw new Error(`invalid ${key}`);
    }
    const ready = JSON.parse(readFileSync(join(root, "keeper-ready.json"), "utf8"));
    if (ready.token !== session.token || !Number.isInteger(ready.pid) || ready.pid <= 0) {
      throw new Error("cleanup keeper did not become ready");
    }
    process.kill(ready.pid, 0);
    // Register before the full frontend starts and before acknowledging a handoff.
    // Keep the record on abrupt/normal exit: the keeper checks OS process handles,
    // so an exit callback is never required for cleanup to work.
    const participant = join(root, "participants", `${process.pid}-${randomUUID()}.json`);
    const temporary = participant + ".tmp";
    if (realpathSync(join(root, "participants")) !== join(root, "participants")) throw new Error("redirected participants");
    writeFileSync(temporary, JSON.stringify({
      token: session.token, pid: process.pid, executable: process.execPath,
      startedAt: Date.now() - process.uptime() * 1000,
    }), {flag: "wx"});
    renameSync(temporary, participant);
    const health = setInterval(() => {
      try { process.kill(ready.pid, 0); }
      catch {
        clearInterval(health);
        process.stderr.write("[gitgo-test] Cleanup keeper stopped. Test state may remain; see .gitgo/terminal-tests.\n");
      }
    }, 2000);
    health.unref();
    return true;
  } catch (error) {
    throw new Error(`LAUNCH_SESSION_FAILED: ${error instanceof Error ? error.message : String(error)}. Test did not start; retry the BAT.`);
  }
}
