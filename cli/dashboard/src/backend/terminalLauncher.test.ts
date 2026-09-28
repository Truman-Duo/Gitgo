import { describe, expect, test } from "bun:test";
import {
  globalConfigPath,
  isShellLauncherProcess,
  shouldRelaunchInConfiguredTerminal,
  terminalChildCommand,
} from "./terminalLauncher.js";

describe("installed terminal launcher", () => {
  test("keeps an invocation from a real terminal attached", () => {
    expect(shouldRelaunchInConfiguredTerminal({
      compiled: true, platform: "win32", argv: ["gitgo.exe"],
      parentProcessName: "powershell.exe",
      config: {terminal: "auto", command: "", args: []},
    })).toBeFalse();
  });

  test("relaunches an Explorer invocation exactly once", () => {
    const config = {terminal: "auto" as const, command: "", args: []};
    expect(isShellLauncherProcess("explorer.exe")).toBeTrue();
    expect(shouldRelaunchInConfiguredTerminal({
      compiled: true, platform: "win32", argv: ["gitgo.exe"],
      parentProcessName: "Explorer", config,
    })).toBeTrue();
    expect(shouldRelaunchInConfiguredTerminal({
      compiled: true, platform: "win32", argv: ["gitgo.exe", "--attached"],
      parentProcessName: "Explorer", config,
    })).toBeFalse();
  });

  test("builds Windows Terminal and configurable terminal child commands", () => {
    expect(terminalChildCommand(
      {terminal: "windows_terminal", command: "", args: []},
      "C:\\Gitgo\\gitgo.exe", ["5"], "C:\\WindowsApps\\wt.exe",
    )).toEqual({
      command: "C:\\WindowsApps\\wt.exe",
      args: ["new-tab", "--", "C:\\Gitgo\\gitgo.exe", "--attached", "5"],
    });
    expect(terminalChildCommand(
      {terminal: "custom", command: "wezterm.exe", args: ["start", "--"]},
      "C:\\Gitgo\\gitgo.exe", [],
    )).toEqual({
      command: "wezterm.exe",
      args: ["start", "--", "C:\\Gitgo\\gitgo.exe", "--attached"],
    });
  });

  test("uses the global config location or the explicit isolation path", () => {
    expect(globalConfigPath({}, "C:\\Users\\Ada")).toBe("C:\\Users\\Ada\\.gitgo\\config.json");
    expect(globalConfigPath({GITGO_CONFIG_PATH: "C:\\Temp\\isolated.json"}, "ignored"))
      .toBe("C:\\Temp\\isolated.json");
  });
});
