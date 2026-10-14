import {describe, expect, test} from "bun:test";
import {EventEmitter} from "node:events";
import {PassThrough} from "node:stream";
import {mkdtempSync, mkdirSync, writeFileSync, rmSync, symlinkSync, realpathSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {pathToFileURL} from "node:url";
import {acquireManagedAdmission, managedRoot} from "./managedAdmission.js";

function fixture() {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "gitgo-admission-")));
  for (const path of ["bootstrap/gitgo-host", "control", "active/dashboard/dist"]) mkdirSync(join(root, path), {recursive: true});
  for (const path of ["bootstrap/gitgo-host/gitgo-host", "bootstrap/gitgo-host/gitgo-host.exe", "control/registration.json", "active/gitgo", "active/gitgo.exe", "active/dashboard/dist/cli.js"]) writeFileSync(join(root, path), "test");
  return root;
}

function fakeSpawn(reply: (nonce: string) => string | "exit") {
  const child = Object.assign(new EventEmitter(), {stdin: new PassThrough(), stdout: new PassThrough(),
    stderr: new PassThrough(), exitCode: null as number | null, signalCode: null, kill() {return true;}});
  // Closing stdin models the stable keeper's EOF release and avoids real timers.
  child.stdin.on("finish", () => {child.exitCode = 0; child.emit("exit", 0);});
  const spawn = (_host: string, args: string[]) => {
    queueMicrotask(() => {
      const data = reply(args[3]);
      if (data === "exit") {child.exitCode = 2; child.emit("exit", 2);}
      else child.stdout.write(data);
    });
    return child;
  };
  return {child, spawn: spawn as any};
}

describe("early managed frontend admission", () => {
  test("physical Bun binary, command symlink and Node companion locate the same installation", () => {
    const root = fixture();
    try {
      symlinkSync(join(root, "active/gitgo"), join(root, "command"));
      expect(managedRoot(join(root, "command"), "file:///not-used.tsx", "darwin")).toBe(root);
      expect(managedRoot(process.execPath, pathToFileURL(join(root, "active/dashboard/dist/cli.js")).href, "linux")).toBe(root);
      expect(managedRoot(process.execPath, import.meta.url, "darwin")).toBeUndefined();
      expect(managedRoot(join(root, "active/gitgo"), import.meta.url, "win32")).toBeUndefined();
      expect(managedRoot(join(root, "active/gitgo.exe"), import.meta.url, "win32")).toBe(root);
    } finally {rmSync(root, {recursive: true, force: true});}
  });
  test("Windows keeper uses the actual exe rather than a Unix host name", async () => {
    const root = fixture();
    const {spawn} = fakeSpawn(nonce => JSON.stringify({schema_version: 1, nonce, version: "1.0.0"}) + "\n");
    try {
      let path = "";
      const checkedSpawn = ((host: string, args: string[], options: any) => {
        path = host; return spawn(host, args, options);
      }) as any;
      const release = await acquireManagedAdmission(root, "1.0.0", checkedSpawn, "win32");
      expect(path).toBe(join(root, "bootstrap/gitgo-host/gitgo-host.exe"));
      release();
    } finally {rmSync(root, {recursive: true, force: true});}
  });
  test("real keeper protocol stays open until explicitly released", async () => {
    const root = fixture();
    const {child, spawn} = fakeSpawn(nonce => JSON.stringify({schema_version: 1, nonce, version: "1.0.0"}) + "\n");
    try {
      const release = await acquireManagedAdmission(root, "1.0.0", spawn);
      expect(child.stdin.writableEnded).toBe(false);
      release(); release();
      expect(child.stdin.writableEnded).toBe(true);
    } finally {rmSync(root, {recursive: true, force: true});}
  });
  for (const kind of ["nonce", "version", "schema", "extra", "null", "oversize", "exit"]) {
    test(`rejects ${kind} without admitting profile initialization`, async () => {
      const root = fixture();
      const {child, spawn} = fakeSpawn(nonce => {
        if (kind === "exit") return "exit";
        if (kind === "null") return "null\n";
        if (kind === "oversize") return "x".repeat(4097);
        const report: any = {schema_version: 1, nonce, version: "1.0.0"};
        if (kind === "nonce") report.nonce = "untrusted";
        if (kind === "version") report.version = "2.0.0";
        if (kind === "schema") report.schema_version = 2;
        if (kind === "extra") report.untrusted = true;
        return JSON.stringify(report) + "\n";
      });
      try {
        await expect(acquireManagedAdmission(root, "1.0.0", spawn)).rejects.toThrow();
        expect(child.stdin.writableEnded).toBe(true);
      } finally {rmSync(root, {recursive: true, force: true});}
    });
  }
  test("an active directory without registered bootstrap cannot silently become unmanaged", async () => {
    const root = fixture();
    try {
      rmSync(join(root, "control/registration.json"));
      await expect(acquireManagedAdmission(root, "1.0.0")).rejects.toThrow("stable bootstrap");
    } finally {rmSync(root, {recursive: true, force: true});}
  });
});
