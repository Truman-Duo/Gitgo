import {test, expect} from "bun:test";
import {mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {createPublishStateStore} from "./publishLocalState.js";

test("a fresh process reads durable Chinese/emoji drafts, isolated by project and workspace, without tokens", () => {
  const root = mkdtempSync(join(tmpdir(), "gitgo-publish-state-"));
  try {
    const store = createPublishStateStore(root), state = store.scope("项目🚀", "C:/workspace");
    state.set("draft", {body: "尚未保存的中文🚀", token: "DO-NOT-PERSIST", nested: {password: "PRIVATE"}, cursor: 4});
    expect(state.error).toBe("");
    const file = join(root, readdirSync(root)[0]!); const raw = readFileSync(file, "utf8");
    expect(raw).not.toContain("DO-NOT-PERSIST"); expect(raw).not.toContain("PRIVATE");
    expect(createPublishStateStore(root).scope("项目🚀", "C:/workspace").get<any>("draft").body).toBe("尚未保存的中文🚀");
    expect(store.scope("另一个项目", "C:/workspace").get("draft")).toBeUndefined();
    expect(store.scope("项目🚀", "D:/workspace").get("draft")).toBeUndefined();
    const script = `import {createPublishStateStore} from ${JSON.stringify(join(import.meta.dir, "publishLocalState.ts"))}; process.stdout.write(createPublishStateStore(${JSON.stringify(root)}).scope("项目🚀", "C:/workspace").get("draft").body);`;
    const child = Bun.spawnSync([process.execPath, "-e", script]);
    expect(child.exitCode).toBe(0); expect(child.stdout.toString()).toBe("尚未保存的中文🚀");
    state.remove("draft"); expect(createPublishStateStore(root).scope("项目🚀", "C:/workspace").get("draft")).toBeUndefined();
    writeFileSync(file, "corrupt"); expect(createPublishStateStore(root).scope("项目🚀", "C:/workspace").error).toBe("PUBLISH_LOCAL_STATE_UNREADABLE");
  } finally { rmSync(root, {recursive: true, force: true}); }
});

test("failed persistence reports the error and keeps the last saved draft", () => {
  const state = createPublishStateStore().scope("project"); state.set("draft", "keep");
  state.set("draft", "x".repeat(1_048_577));
  expect(state.error).toBe("PUBLISH_LOCAL_STATE_FULL"); expect(state.get<string>("draft")).toBe("keep");
});
