import { expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { registerLaunchSession } from "./launchSession.js";

test("ordinary launches do not create temporary lifecycle records", () => {
  expect(registerLaunchSession({})).toBeFalse();
});

test("temporary dashboards register atomically and reject a global config path", () => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "gitgo-launch-session-")));
  const token = randomUUID();
  const env = {GITGO_LAUNCH_SESSION: join(root, "session.json"), GITGO_LAUNCH_SESSION_TOKEN: token,
    GITGO_CONFIG_PATH: join(root, "config.json"), GITGO_STATE_HOME: join(root, "state"),
    GITGO_LLM_CONFIG_PATH: join(root, "llm_config.json"), GITGO_LLM_SECRET_PATH: join(root, "provider_secrets.json")};
  mkdirSync(join(root, "participants"));
  writeFileSync(env.GITGO_LAUNCH_SESSION, JSON.stringify({version: 1, root, token, executable: process.execPath}));
  writeFileSync(join(root, "keeper-ready.json"), JSON.stringify({token, pid: process.pid}));
  try {
    expect(() => registerLaunchSession({...env, GITGO_CONFIG_PATH: join(tmpdir(), "global.json")})).toThrow("invalid GITGO_CONFIG_PATH");
    expect(() => registerLaunchSession({...env, GITGO_LAUNCH_SESSION_TOKEN: randomUUID()})).toThrow("invalid session identity");
    expect(registerLaunchSession(env)).toBeTrue();
    const files = readdirSync(join(root, "participants"));
    expect(files.length).toBe(1);
    expect(files[0]?.endsWith(".json")).toBeTrue();
    const record = JSON.parse(readFileSync(join(root, "participants", files[0]!), "utf8"));
    expect(record.pid).toBe(process.pid);
    expect(record.token).toBe(token);
    expect(record.startedAt).toBeLessThanOrEqual(Date.now());
  } finally { rmSync(root, {recursive: true}); }
});
