"""Local, opt-in cross-version update probe for a disposable SIP-enabled VM.

Run with an independent release's frozen Host, never the installation being
updated. ``prepare`` replaces only HTTP transport with local signed fixtures;
the installed policy, verifier, package contract and preparation service remain
real. ``ui`` drives the public command through a PTY and never calls the private
transaction engine. Public HTTPS/CDN and production publisher trust are NOT
covered. Caller supplies an isolated HOME/config/state environment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os

# Keep report/fixture verification importable without native PTY support.
if os.name == 'posix':
    import fcntl
    import pty
    import termios

from pathlib import Path
import re
import select
import signal
import stat
import struct
import subprocess
import sys
import time
import uuid


def require(value, message):
    if not value:
        raise RuntimeError(message)


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def guard(root, command):
    require(sys.platform == "darwin" and getattr(sys, "frozen", False),
            "Use an independent macOS package's frozen Host")
    require(501 <= os.getuid() < 200000000 and os.getuid() == os.geteuid(),
            "Use an ordinary VM login account, not root")
    require(subprocess.check_output(["/usr/sbin/sysctl", "-n", "hw.model"], text=True).strip().startswith("VirtualMac"),
            "This probe runs only in a disposable macOS VM")
    require(subprocess.check_output(["/usr/bin/csrutil", "status"], text=True).strip()
            == "System Integrity Protection status: enabled.", "SIP must be fully enabled")
    require(root.is_relative_to(Path("/private/var/tmp")) and not root.is_symlink(),
            "Use an isolated managed installation below /private/var/tmp")
    info = root.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "Managed test root must be private to this user")
    require(not Path(sys.executable).resolve().is_relative_to(root),
            "Probe Host must be outside the installation so it holds no App runtime lease")
    require(command.resolve(strict=True) == root / "gitgo", "Use this installation's original public command")
    for name in ("HOME", "GITGO_CONFIG_PATH", "GITGO_STATE_HOME", "GITGO_LLM_CONFIG_PATH", "GITGO_LLM_SECRET_PATH"):
        value = Path(os.environ.get(name, "")).absolute()
        require(value.is_relative_to(root.parent) and not value.is_relative_to(root),
                "Provide isolated profile paths outside the managed program: " + name)


def new_artifacts(path):
    require(not path.is_symlink(), "Artifact directory cannot be a symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077 and not any(path.iterdir()),
            "Artifact directory must be private, new or empty")
    return path.resolve(strict=True)


def inspect_store(root):
    from backend.core.update.installation import open_managed
    from backend.core.update.tree import tree_digest
    from backend.core.update.handoff import strategy
    from backend.core.update.deferred import pending_status
    from backend.core.update.persistence import read_control
    store = open_managed(root)
    ledger = store._ledger()
    transaction = ledger["transaction"]
    health_failure = None
    if transaction is not None:
        # The validated ledger supplies the bounded transaction ID. _slots
        # also checks the private transaction directory before the existing
        # no-follow, owner/mode/size-bounded control reader opens evidence.
        evidence = store._slots(transaction)[1].parent / "health-failure.json"
        if evidence.exists() or evidence.is_symlink():
            health_failure = read_control(evidence)
    return {"current": ledger["current"], "rollback": store.rollback_selection(),
            "transaction": transaction, "health_failure": health_failure, "watermarks": ledger["watermarks"],
            "active_tree_sha256": tree_digest(root / "active"),
            "bootstrap_api": store.registration["bootstrap_api"],
            "strategy": strategy(store), "pending": pending_status(store)}


def fixture_response(httpx, request, resources):
    """Return only exact locally mapped URLs; never fall through to the network."""
    path = resources.get(str(request.url))
    if request.method != "GET" or path is None:
        return httpx.Response(404)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(64 * 1024), b""):
                    yield chunk

    return httpx.Response(200, headers={"Content-Length": str(path.stat().st_size)}, stream=Stream())


def prepare(args):
    import httpx
    from backend.core.update.installation import open_managed
    from backend.core.update.release_policy import installed_release_policy
    from backend.core.update.preparation_service import UpdatePreparationService
    from backend.core.update.transport import HttpsTransport
    from backend.core.update.automatic import preferences
    store = open_managed(args.root)
    policy = installed_release_policy(store)
    require(policy is not None, "Managed installation has no pinned release policy")
    raw, signature = args.manifest.read_bytes(), args.signature.read_bytes()
    # Derive the package URL only from authenticated metadata. No request may
    # replace the installation's pinned key, allowed hosts or channel URLs.
    manifest = policy.verifier.verify(raw, signature)
    channel = policy.channel(args.channel)
    resources = {channel.manifest_url: args.manifest, channel.signature_url: args.signature,
                 manifest.url: args.archive}
    require(len(resources) == 3, "Fixture metadata and package URLs must be distinct")
    calls = []

    def serve(request):
        calls.append(str(request.url))
        return fixture_response(httpx, request, resources)

    service = UpdatePreparationService(store_loader=lambda: open_managed(args.root),
        transport_factory=lambda hosts, **options: HttpsTransport(hosts,
            transport=httpx.MockTransport(serve), **options))
    before = inspect_store(args.root)
    try:
        # Avoid a background network refresh replacing the deliberately cached
        # selection, and avoid automatic installation during version probes.
        # This is the normal preference operation, not a control-file edit.
        settings = preferences(store)
        service.configure(args.channel, False, False, False, settings["revision"])
        checked = service.check(args.channel, uuid.uuid4().hex)
        require(checked.get("candidate") is not None, "No newer compatible signed fixture candidate")
        selected = checked["candidate"]["candidate_id"]
        downloaded = service.download(args.channel, selected, uuid.uuid4().hex)
        require(downloaded["status"] == "downloaded", "Signed fixture was not prepared")
        after = inspect_store(args.root)
        require(after["current"] == before["current"] and
                after["active_tree_sha256"] == before["active_tree_sha256"],
                "Preparation changed the running program")
        return {"passed": True, "operation": "prepare", "network": "injected-local-fixture-only",
                "publisher_trust_verified": False, "manifest_sha256": sha256(args.manifest),
                "signature_sha256": sha256(args.signature), "archive_sha256": sha256(args.archive),
                "version": manifest.version, "candidate_id": selected, "calls": calls,
                "download_status": downloaded["status"], "before": before, "after": after}
    finally:
        service.close()


def terminal_reader(path):
    spec = importlib.util.spec_from_file_location("gitgo_cross_version_terminal_reader", path)
    require(spec is not None and spec.loader is not None, "Terminal reader module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def final_update_result(output):
    """Read the launcher's final JSON result from bounded PTY output."""
    require(len(output) <= 4 * 1024 * 1024, "Bounded terminal output exceeded limit")
    text = output.decode("utf-8", "replace")
    decoder = json.JSONDecoder()
    result = None
    for match in re.finditer(r'\{"update_status"\s*:', text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if type(value) is dict:
            result = value
    require(result is not None, "Managed launcher did not emit a complete update result")
    return result


def check_update_observation(before, after, result, expected):
    require(result.get("update_status") == expected, "Final launcher update status differs from expected")
    if expected in ("committed", "rolled_back"):
        transaction = after["transaction"]
        require(transaction is not None and transaction["state"] == expected.upper(),
                "Durable transaction state differs from the public update result")
        if expected == "committed":
            require(transaction["failure_code"] is None, "Committed transaction retained a failure code")
    elif expected == "blocked":
        require(result.get("error_code") == "UPDATE_BUSY", "Expected runtime-lease refusal, not another blocker")
        require(all(before[key] == after[key] for key in ("current", "transaction", "active_tree_sha256")),
                "Busy update changed the program or durable transaction")


def ui(args, artifacts):
    from backend.core.update.deferred import boot_session
    from backend.core.update.installation import open_managed
    reader = terminal_reader(args.terminal_reader)
    boot = boot_session()
    before = inspect_store(args.root)
    require(before["strategy"] == "immediate", "This test needs the already-fixed macOS quit updater")
    config_path = Path(os.environ["GITGO_CONFIG_PATH"])
    config_before = config_path.read_bytes() if config_path.exists() else None
    if config_before is not None:
        config = json.loads(config_before)
        require(config.get("language", "en") == "en", "This bounded terminal reader requires the isolated English profile")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    environment = {**os.environ, "TERM": "xterm-256color", "FORCE_COLOR": "3"}
    environment.pop("NO_COLOR", None)
    proc = subprocess.Popen([str(args.command), "--attached"], stdin=slave, stdout=slave, stderr=slave,
                            env=environment, start_new_session=True)
    os.close(slave)
    output = bytearray()
    screen = reader.TerminalScreen(40, 120)
    log = artifacts / "dashboard.terminal"

    def pump():
        if select.select([master], [], [], .1)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            require(bool(chunk), "Dashboard closed before expected state: " + screen.transcript()[-1600:])
            output.extend(chunk)
            require(len(output) <= 4 * 1024 * 1024, "Bounded terminal output exceeded limit")
            screen.feed(chunk)
        else:
            require(proc.poll() is None, "Dashboard exited before expected state")

    def wait_for(text, *, after=0, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            displayed = output[after:].decode("utf-8", "replace") if '"update_status"' in text else screen.transcript()
            if text in displayed and len(output) > after:
                return
            require(time.monotonic() < deadline,
                    "Dashboard did not reach " + repr(text) + ": " + screen.transcript()[-1600:])
            pump()

    def send(value):
        reader.settle_screen(screen, pump)
        os.write(master, value.encode())

    try:
        deadline = time.monotonic() + 90
        while True:
            text = screen.text()
            if "Providers" in text and "General" in text:
                send("\x1b")
                screen.forget_history()
            if "Type / for commands" in screen.text():
                break
            require(time.monotonic() < deadline, "Dashboard project list was not ready: " + screen.transcript()[-1600:])
            pump()
        screen.forget_history()
        start = len(output)
        send("/")
        wait_for("/archive", after=start)
        prefix = "/"
        for letter in "update":
            start = len(output)
            send(letter)
            prefix += letter
            wait_for(prefix, after=start)
        require(not termios.tcgetattr(master)[0] & termios.ICRNL, "PTY still translates Enter to newline")
        start = len(output)
        send("\r")
        wait_for("Gitgo Software Update", after=start)
        if args.action == "apply":
            wait_for("Downloaded and verified")
            reader.activate_menu_item(screen, "Review restart / update", send, pump)
            wait_for("attempt the selected update?")
        else:
            wait_for("Retained version")
            reader.activate_menu_item(screen, "Review rollback", send, pump)
            wait_for("Close this session and roll back?")
        screen.forget_history()
        start = len(output)
        send("\r")
        wait_for('"update_status": "' + args.expect_status + '"', after=start, timeout=150)
        proc.wait(timeout=30)
        require(proc.returncode == (2 if args.expect_status == "blocked" else 0),
                "Unexpected managed launcher return code")
        require(b"restart_required" not in output, "Update unexpectedly required a macOS restart")
    finally:
        log.write_bytes(output)
        if proc.poll() is None:
            try:
                os.write(master, b"\x03")
                proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        os.close(master)

    after = inspect_store(args.root)
    result = final_update_result(output)
    check_update_observation(before, after, result, args.expect_status)
    require(after["pending"] is None, "A deferred restart request remains")
    require(after["current"]["version"] == args.expect_version, "App current identity differs from expected version")
    require(boot_session() == boot, "macOS rebooted during the quit-only update")
    reopened = subprocess.run([str(args.command), "--version"], capture_output=True, text=True, timeout=90)
    (artifacts / "reopen.stdout").write_text(reopened.stdout)
    (artifacts / "reopen.stderr").write_text(reopened.stderr)
    require(reopened.returncode == 0 and reopened.stdout.strip() == "Gitgo " + args.expect_version,
            "Original public entry did not reopen the expected version")
    # A stable entry must still name the same installation after activation.
    require(args.command.resolve(strict=True) == args.root / "gitgo", "Public entry changed during update")
    require(open_managed(args.root)._ledger()["current"] == after["current"],
            "Reopening unexpectedly changed the current version")
    return {"passed": True, "operation": "ui", "action": args.action,
            "status": args.expect_status, "launcher_result": result, "boot_identity": boot, "same_boot": True,
            "public_command": str(args.command), "reopened_version": reopened.stdout.strip(),
            "terminal_reader_sha256": sha256(args.terminal_reader),
            "terminal_sha256": sha256(log), "before": before, "after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--allow-local-adhoc", action="store_true")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--command", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    sub = parser.add_subparsers(dest="operation", required=True)
    prep = sub.add_parser("prepare")
    for name in ("manifest", "signature", "archive"):
        prep.add_argument("--" + name, required=True, type=Path)
    prep.add_argument("--channel", choices=("preview", "stable"), default="preview")
    panel = sub.add_parser("ui")
    panel.add_argument("--action", choices=("apply", "rollback"), required=True)
    panel.add_argument("--expect-status", choices=("committed", "rolled_back", "blocked"), required=True)
    panel.add_argument("--expect-version", required=True)
    panel.add_argument("--terminal-reader", required=True, type=Path)
    sub.add_parser("inspect")
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        raise RuntimeError("Cross-version update probe interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    require(args.run and args.allow_local_adhoc, "Use --run --allow-local-adhoc explicitly")
    args.root = args.root.resolve(strict=True)
    args.command = args.command.absolute()
    guard(args.root, args.command)
    artifacts = new_artifacts(args.artifact_dir.absolute())
    try:
        if args.operation == "prepare":
            report = prepare(args)
        elif args.operation == "ui":
            report = ui(args, artifacts)
        else:
            report = {"passed": True, "operation": "inspect", **inspect_store(args.root)}
    except Exception as exc:
        report = {"passed": False, "operation": args.operation, "error": type(exc).__name__ + ": " + str(exc)}
    report.update(harness_sha256=sha256(Path(__file__)), production_enabled=False,
                  publisher_trust_verified=False, artifact_dir=str(artifacts))
    (artifacts / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
