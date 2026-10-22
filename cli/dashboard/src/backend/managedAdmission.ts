// The stable Host owns the actual flock. Never treat an env flag or a numeric
// descriptor asserted by the caller as proof that a frontend has a lease.
import {spawn} from "node:child_process";
import {randomUUID} from "node:crypto";
import {existsSync, realpathSync} from "node:fs";
import {basename, dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";

export function managedRoot(executable: string, entryUrl: string, platform = process.platform): string | undefined {
  let active: string | undefined;
  try {
    const physical = realpathSync(executable);
    if (basename(dirname(physical)) === "active" && basename(physical) === (platform === "win32" ? "gitgo.exe" : "gitgo")) active = dirname(physical);
    if (!active && entryUrl.startsWith("file:")) {
      const entry = realpathSync(fileURLToPath(entryUrl));
      if (basename(entry) === "cli.js" && basename(dirname(entry)) === "dist"
          && basename(dirname(dirname(entry))) === "dashboard") {
        const candidate = resolve(dirname(entry), "../..");
        if (basename(candidate) === "active") active = candidate;
      }
    }
  } catch { return; }
  return active ? dirname(active) : undefined;
}

export async function acquireManagedAdmission(root: string, version: string, spawnHost = spawn, platform = process.platform): Promise<() => void> {
  const host = resolve(root, "bootstrap/gitgo-host", platform === "win32" ? "gitgo-host.exe" : "gitgo-host");
  if (!existsSync(host) || !existsSync(resolve(root, "control/registration.json"))) {
    throw new Error("Managed installation requires its stable bootstrap; use the installed public command");
  }
  const nonce = randomUUID().replaceAll("-", "");
  const child = spawnHost(host, ["--gitgo-internal-role", "managed-front-lease", "--nonce", nonce], {
    cwd: dirname(host), env: process.env, stdio: ["pipe", "pipe", "pipe"],
  });
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    child.stdin?.end();
    const timer = setTimeout(() => { if (child.exitCode === null) child.kill(); }, 5000);
    timer.unref(); child.once("exit", () => clearTimeout(timer));
  };
  try {
    await new Promise<void>((yes, no) => {
      let data = "";
      let settled = false;
      const timer = setTimeout(() => done(new Error("Managed startup lease timed out")), 30000);
      const done = (error?: Error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer); child.stdout?.off("data", read);
        child.off("error", failed); child.off("exit", exited);
        error ? no(error) : yes();
      };
      const failed = () => done(new Error("Managed startup lease failed"));
      const exited = () => done(new Error("Managed installation rejected this startup"));
      const read = (chunk: Buffer) => {
        data += chunk.toString("utf8");
        if (Buffer.byteLength(data, "utf8") > 4096) return done(new Error("Managed startup lease report exceeded its limit"));
        if (!data.includes("\n")) return;
        try {
          const result = JSON.parse(data.slice(0, data.indexOf("\n")));
          if (!result || typeof result !== "object" || Array.isArray(result)
              || Object.keys(result).sort().join(",") !== "nonce,schema_version,version"
              || result.schema_version !== 1 || result.nonce !== nonce || result.version !== version) {
            throw new Error("mismatched admission");
          }
          done();
        } catch { done(new Error("Managed startup lease was not verified; restart from the public command")); }
      };
      child.stdout?.on("data", read); child.stderr?.on("data", () => {});
      child.once("error", failed); child.once("exit", exited);
    });
    if (child.exitCode !== null || child.signalCode !== null) throw new Error("Managed startup lease ended before admission");
    child.once("exit", () => {
      if (!released) {
        process.stderr.write("Managed installation lease ended unexpectedly; stopping this frontend\n");
        process.exit(1);
      }
    });
    process.once("exit", release);
    return () => { process.off("exit", release); release(); };
  } catch (error) { release(); throw error; }
}
