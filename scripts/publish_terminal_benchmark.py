"""Measure the packaged application through a real, visible Windows console.

Only read-only Publish views and unsent command text are exercised. Each sample
starts at the actual key injection and ends at an observed screen condition;
command preparation, setup sleeps and screenshots are outside the measurement.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from publish_acceptance_terminal import Console, console_client
from publish_packaged_terminal_review import descendants


class LiveConsole(Console):
    def screen(self):
        # The application enters the alternate screen after startup. A handle
        # opened before launch can still refer to the old, now inactive buffer.
        # Reopen CONOUT$ for observation without activating/changing any buffer.
        fresh = self.kernel.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        if fresh in (None, C.c_void_p(-1).value):
            raise OSError("Active console buffer unavailable")
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        previous, self.output = self.output, fresh
        self.kernel.CloseHandle(previous)
        return super().screen()


def summary(samples, budget=None):
    values = sorted(samples)
    return {"samples": len(values), "min_ms": min(values), "max_ms": max(values),
            "median_ms": statistics.median(values),
            "p95_ms": values[math.ceil(.95 * len(values)) - 1],
            **({"budget_ms": budget, "passed": values[math.ceil(.95 * len(values)) - 1] <= budget}
               if budget is not None else {})}


def run(package, fixture, root, samples, startup_samples):
    desktop = (Path.home() / "Desktop").resolve()
    package, fixture, root = (Path(p).resolve(strict=True) for p in (package, fixture, root))
    if not all(p.is_relative_to(desktop) for p in (package, fixture, root)):
        raise ValueError("Package, fixture and evidence must remain on Desktop")
    if not 20 <= samples <= 100 or not 3 <= startup_samples <= 20:
        raise ValueError("Use 20-100 interaction samples and 3-20 process starts")
    evidence = Path(tempfile.mkdtemp(prefix="terminal-performance-", dir=root))
    exe = package / "gitgo.exe"
    host = package / "internal/gitgo-host/gitgo-host.exe"
    config = json.loads((fixture / "config.json").read_text(encoding="utf8"))
    project = config["projects"][0]["name"].partition("🚀")[0]
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("GITGO_", "PYTHON"))}
    env.update(GITGO_INSTALL_ROOT=str(package), GITGO_STATE_HOME=str(fixture / "state"),
               GITGO_CONFIG_PATH=str(fixture / "config.json"), GITGO_LLM_CONFIG_PATH=str(fixture / "llm.json"),
               FORCE_COLOR="3", TERM="xterm-256color")
    env.pop("NO_COLOR", None)
    report = {"schema": "gitgo.publish.terminal-performance.v1", "evidence": str(evidence),
              "package": str(package), "fixture": str(fixture), "samples_per_interaction": samples,
              "product_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
              "host_sha256": hashlib.sha256(host.read_bytes()).hexdigest(), "startup_ms": [], "sizes": {},
              "measurement": "monotonic key injection to visible console-buffer condition; 5 ms polling; nearest-rank p95",
              "startup_scope": "Fresh frontend and Host processes; OS filesystem caches are not flushed. Console setup precedes launch.",
              "limits": ["Console buffer observation is not a display-panel photon measurement",
                         "This completed fixture has one item and short history; the separate 1000-item/10000-commit report measures scale"],
              "completed": False}
    output = evidence / "result.json"

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")

    print(json.dumps({"evidence": str(evidence)}), flush=True)
    try:
        for trial in range(startup_samples):
            target = evidence / f"startup-{trial}"
            target.mkdir()
            gate, launched = target / "start", target / "launched.json"
            launcher = target / "launch.py"
            launcher.write_text("import json,pathlib,subprocess,time\n"
                + "while not pathlib.Path(" + repr(str(gate)) + ").exists(): time.sleep(.005)\n"
                + "started=time.monotonic()\n"
                + "p=subprocess.Popen(" + repr([str(exe), "--attached"]) + ")\n"
                + "pathlib.Path(" + repr(str(launched)) + ").write_text(json.dumps({'started':started,'pid':p.pid}))\n"
                + "p.wait()\n", encoding="utf8")
            startup = subprocess.STARTUPINFO()
            startup.dwFlags, startup.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 1
            process = subprocess.Popen([str(Path(os.environ["SystemRoot"]) / "System32/conhost.exe"),
                                        sys.executable, str(launcher)], cwd=package, env=env,
                                       creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
            console = None
            try:
                console = LiveConsole(process, target, console_client(process), visible=True)
                console.resize(120, 32)

                def wait(predicate, timeout=15):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        text = console.screen()[0]
                        if predicate(text):
                            return time.monotonic(), text
                        if process.poll() is not None:
                            raise RuntimeError("Owned application console exited")
                        time.sleep(.005)
                    console.capture("timeout")
                    raise TimeoutError("Expected screen condition was not observed")

                def measure(action, predicate, timeout=15):
                    started = time.monotonic()
                    action()
                    observed, _ = wait(predicate, timeout)
                    return round((observed - started) * 1000, 3)

                gate.write_text("start", encoding="ascii")
                observed, _ = wait(lambda text: "项目名称" in text and project in text, 60)
                launched_data = json.loads(launched.read_text(encoding="utf8"))
                report["startup_ms"].append(round((observed - launched_data["started"]) * 1000, 3))
                console.capture("project-ready")
                if trial > 0:
                    continue
                report["process_tree"] = descendants(process.pid)
                for executable in (exe, host):
                    assert any(row.get("path", "").casefold() == str(executable).casefold()
                               for row in report["process_tree"]), str(executable)
                report["console"] = console.metrics()
                console.send("\r")
                time.sleep(1)

                def prepare_command(command):
                    # '/' changes input mode; let that render before typing the
                    # command. A single pasted '/command' is ordinary chat text.
                    console.send("/")
                    console.send(command)
                    wait(lambda text: "/" + command in text)

                prepare_command("publish")
                report["publish_first_open_ms"] = measure(lambda: console.key("\r", 13),
                    lambda text: "本地已保存记录" in text and "HEAD" in text and "正在读取记录" not in text)
                # Publish initially focuses its header. One Enter selects items.
                console.send("\r")
                wait(lambda text: "[核对]" in text)
                for width, height in ((120, 32), (80, 24), (40, 20)):
                    console.resize(width, height)
                    time.sleep(.2)
                    row = {"step_ms": [], "scroll_ms": [], "input_ms": [], "history_open_ms": []}
                    report["sizes"][f"{width}x{height}"] = row
                    for index in range(samples):
                        left = index % 2 == 0
                        row["step_ms"].append(measure(lambda: console.key(virtual=37 if left else 39),
                            lambda text, expected="[记录]" if left else "[核对]": expected in text))
                    # Never submit the temporary command text.
                    console.key("/")
                    wait(lambda text: any(line.strip() == "/" for line in text.splitlines()))
                    for index in range(samples):
                        letter = chr(97 + index % 26)
                        row["input_ms"].append(measure(lambda: console.key(letter),
                            lambda text, expected="/" + letter: any(line.strip() == expected for line in text.splitlines())))
                        console.key("\b", 8)
                        wait(lambda text: any(line.strip() == "/" for line in text.splitlines()))
                    console.key("\x1b", 27)
                    wait(lambda text: not any(line.strip() == "/" for line in text.splitlines()))
                    console.send("\r")
                    wait(lambda text: "返回事项" in text)
                    for index in range(samples):
                        # Alternate down/up so each sample has actual content to scroll.
                        before = console.screen()[0].splitlines()[3:height - 7]
                        row["scroll_ms"].append(measure(lambda: console.key(virtual=40 if index % 2 == 0 else 38),
                            lambda text, previous=before: text.splitlines()[3:height - 7] != previous))
                        time.sleep(.02)
                    for index in range(samples):
                        prepare_command("history")
                        row["history_open_ms"].append(measure(lambda: console.key("\r", 13),
                            lambda text: "Git 提交父关系" in text and "› " in text and "正在读取固定" not in text))
                        console.key("\x1b", 27)
                        wait(lambda text: "Git 提交父关系" not in text and "返回事项" in text)
                    console.capture(f"{width}x{height}-complete")
                    row["summary"] = {name: summary(values, 300 if name == "history_open_ms" else 100)
                                      for name, values in row.items() if name.endswith("_ms")}
                    row["history_first_open_ms"] = row["history_open_ms"][0]
                    row["history_first_open_budget_ms"] = 1000
                    row["history_first_open_passed"] = row["history_first_open_ms"] <= 1000
                    print(json.dumps({"size": f"{width}x{height}", "summary": row["summary"]}), flush=True)
                    console.key("\x1b", 27)
                    wait(lambda text: "[核对]" in text)
                    save()
            finally:
                if console:
                    console.close()
                elif process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                save()
        report["startup_summary"] = summary(report["startup_ms"])
        report["publish_first_open_budget_ms"] = 1000
        report["passed"] = report["publish_first_open_ms"] <= 1000 and all(
            row["history_first_open_passed"] and all(result["passed"] for result in row["summary"].values())
            for row in report["sizes"].values())
        report["completed"] = True
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        save()
    print(json.dumps({"result": str(output), "passed": report["passed"],
                      "startup": report["startup_summary"], "publish_first_open_ms": report["publish_first_open_ms"]}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--startup-samples", type=int, default=3)
    args = parser.parse_args()
    run(args.package, args.fixture, args.root, args.samples, args.startup_samples)
