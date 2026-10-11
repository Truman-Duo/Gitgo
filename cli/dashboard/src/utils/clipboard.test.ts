import { expect, test } from "bun:test";
import { execFileSync } from "node:child_process";
import { WINDOWS_CLIPBOARD_COMMAND } from "./clipboard.js";

// Exercise the actual Windows output adapter without changing the user's
// clipboard. The fixture replaces only Get-Clipboard in the child process.
const windowsTest = process.platform === "win32" ? test : test.skip;
for (const text of [
  "个人离线 Markdown 阅读器，先给完整方案，其余自行决定，不要再问 ",
  "第一行\r\n第二行\r\n",
  "\n首尾有换行\n\n",
  "",
]) {
  windowsTest(`Windows clipboard output preserves ${JSON.stringify(text)}`, () => {
    const base64 = Buffer.from(text, "utf8").toString("base64");
    const fixture = `function Get-Clipboard { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${base64}')) }; `;
    const output = execFileSync("powershell", ["-NoProfile", "-NonInteractive", "-Command",
      fixture + WINDOWS_CLIPBOARD_COMMAND], {encoding: "utf8", timeout: 10000, windowsHide: true});
    expect(output).toBe(text);
  });
}
