"""Unix PTY regression tests. Physical keyboard/IME acceptance is still manual."""
import fcntl
import argparse
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
import time
import tempfile

ROOT = Path(__file__).resolve().parents[1] / "cli/dashboard"


def drain(master, output, duration=0.15):
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.02)
        if ready:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                break


def run(runtime, command, native=False):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    output = bytearray()
    env = dict(os.environ, TERM="xterm-256color", FORCE_COLOR="1")
    proc = subprocess.Popen(command, cwd=ROOT, stdin=slave, stdout=slave,
                            stderr=subprocess.PIPE, env=env, start_new_session=True)
    try:
        drain(master, output, 2 if native else 0.5)
        assert proc.poll() is None, f"{runtime} failed startup"
        assert not (termios.tcgetattr(slave)[3] & termios.ICANON), f"{runtime}: raw mode not enabled"
        for text in ([] if native else [b"\x07", b"\x1b[13;2u", b"\x1b[99;9u",
                     b"\x1b[200~" + "中文\n𠮷".encode() + b"\x1b[201~", b"\x03"]):
            os.write(master, text)
            drain(master, output)
        for byte in (b"" if native else "输入𠮷".encode()):
            os.write(master, bytes([byte]))
        drain(master, output)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 80, 0, 0))
        proc.send_signal(signal.SIGWINCH)
        drain(master, output)
        proc.send_signal(signal.SIGTERM)
        drain(master, output)
        _, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 143, (runtime, proc.returncode, stderr.decode())
        assert termios.tcgetattr(slave)[3] == original[3], f"{runtime}: terminal mode not restored"
        assert b"\x1b[?2004l" in output, f"{runtime}: bracketed paste not disabled"
        if not native:
            events = [json.loads(line) for line in stderr.decode().splitlines() if line.startswith("{")]
            assert {"ready": True} in events
            assert {"cmdCopy": True} in events
            assert any(e.get("ctrl") and e.get("input") == "g" for e in events)
            assert any(e.get("shift") and e.get("enter") for e in events)
            assert any(e.get("paste") and e.get("input") == "中文\n𠮷" for e in events)
            assert "".join(e.get("input", "") for e in events if not e.get("paste") and not e.get("ctrl") and not e.get("enter")) == "输入𠮷", events
            assert {"size": [80, 30]} in events, events
        else:
            assert b"Native host mode" in stderr, stderr.decode()
        print(f"PASS {runtime}: " + ("Native Host startup, resize, SIGTERM and terminal restoration" if native else "raw mode, Ctrl/Cmd sequences, UTF-8, paste, resize, SIGTERM and restoration"))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)
        os.close(slave)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--native-host')
    parser.add_argument('--native-command')
    args = parser.parse_args()
    subprocess.run([os.environ.get("GITGO_BUN", "bun"), "build", "scripts/terminal-probe.tsx",
                    "--target=node", "--outfile=.runtime-tests/terminal-probe.js"], cwd=ROOT, check=True)
    for runtime in ("bun", "node"):
        run(runtime, [os.environ.get(f"GITGO_{runtime.upper()}", runtime), ".runtime-tests/terminal-probe.js"])
    if args.native_host:
        os.environ['GITGO_HOST_EXECUTABLE'] = str(Path(args.native_host).resolve())
        with tempfile.TemporaryDirectory(prefix='gitgo-runtime-config-') as config:
            os.environ['GITGO_CONFIG_PATH'] = str(Path(config) / 'config.json')
            for runtime in ('bun', 'node'):
                command = ([args.native_command, '--runtime', runtime] if args.native_command else
                           ['node', 'scripts/launch.mjs', '--runtime', runtime])
                run(f'{runtime} native', command, native=True)
