"""Host-owned terminal inventory and launcher preference resolution.

Detection never launches a terminal or reads provider credentials. A terminal
hosts Gitgo's UI; it does not select the shell used by Agent command tools.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

TERMINAL_IDS = {"auto", "current", "windows_terminal", "wezterm", "alacritty",
                "conemu", "mintty", "git_bash", "git_bash_console", "gnome_terminal", "konsole", "xterm", "custom"}


def git_install_roots(environment=None):
    env = os.environ if environment is None else environment
    roots = []
    if sys.platform == "win32":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"Software\GitForWindows", 0, winreg.KEY_READ | view) as key:
                        roots.append(Path(winreg.QueryValueEx(key, "InstallPath")[0]))
                except OSError:
                    pass
    git = resolve_executable("git.exe", env)
    if git:
        path = Path(git)
        roots.append(path.parent.parent if path.parent.name.lower() in {"cmd", "bin"} else path.parent)
    for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if env.get(key):
            roots.extend([Path(env[key]) / "Git", Path(env[key]) / "Programs/Git"])
    return list(dict.fromkeys(r.resolve() for r in roots if r.is_absolute()))


def resolve_executable(command: str, environment=None, warnings=None) -> str:
    environment = os.environ if environment is None else environment
    candidate = Path(str(command or "").strip().strip('"')).expanduser()
    if not str(command or "").strip():
        return ""
    def usable(path):
        try:
            return path.is_file()
        except OSError:
            warning = "Some terminal locations could not be inspected; other install locations were checked."
            if warnings is not None and warning not in warnings:
                warnings.append(warning)
            return False
    if candidate.is_absolute():
        return str(candidate) if usable(candidate) else ""
    # Never implicitly search cwd, relative PATH entries or a project binary.
    if candidate.name != str(candidate):
        return ""
    for raw in environment.get("PATH", "").split(os.pathsep):
        directory = Path(raw.strip().strip('"'))
        if not raw.strip() or not directory.is_absolute() or directory.resolve() == Path.cwd().resolve():
            continue
        target = directory / candidate
        if usable(target):
            return str(target)
    return ""


def _registered_paths(names):
    if sys.platform != "win32":
        return []
    import winreg
    paths = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for name in names:
                try:
                    with winreg.OpenKey(hive, "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + name,
                                        0, winreg.KEY_READ | view) as key:
                        value = winreg.QueryValueEx(key, "")[0]
                        if isinstance(value, str):
                            paths.append(value.strip().strip('"'))
                except OSError:
                    pass
    return paths


def _terminal_packages(environment):
    """Windows Terminal may be installed with its wt execution alias disabled."""
    try:
        from backend.core.executable_identity import windows_system_executable
        powershell = windows_system_executable("System32/WindowsPowerShell/v1.0/powershell.exe")
        if not powershell.is_file():
            return [], "Windows Terminal package verifier is unavailable; other locations were checked."
        result = subprocess.run([str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            "@(Get-AppxPackage -Name Microsoft.WindowsTerminal* | Select-Object -ExpandProperty InstallLocation) | ConvertTo-Json -Compress"],
            stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000, timeout=4)
        if result.returncode:
            return [], "Windows Terminal package lookup failed; other detection methods remain available."
        if len(result.stdout) > 65536:
            return [], "Windows Terminal package lookup exceeded its limit."
        values = json.loads(result.stdout or "null")
        if isinstance(values, str):
            values = [values]
        return [str(Path(p) / "WindowsTerminal.exe") for p in (values or []) if isinstance(p, str)], ""
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return [], "Windows Terminal package lookup unavailable; PATH and known install locations were checked."


def terminal_inventory(launcher=None, *, platform=None, environment=None, registered_paths=None, package_paths=None,
                       git_roots=None, git_verifier=None, terminal_verifier=None, windows_build=None):
    platform = sys.platform if platform is None else platform
    env = os.environ if environment is None else environment
    launcher = dict(launcher or {})
    choices = [
        {"id": "auto", "label": "Automatic", "available": True, "command": "", "args": []},
        {"id": "current", "label": "Current terminal", "available": True, "command": "", "args": []},
    ]
    warnings = []
    reference_count = 0
    if platform == "win32":
        from backend.core.package_provenance import load_references
        try:
            reference_count = len(load_references())
            if not reference_count:
                warnings.append("Git terminal package references are unavailable; Git Bash cannot be verified.")
        except (OSError, ValueError, KeyError, TypeError) as error:
            warnings.append(f"Git terminal package references are invalid: {str(error)[:300]}")
    roots = [Path(env[k]) for k in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)") if env.get(k)]
    specs = [
        ("wezterm", "WezTerm", ["wezterm-gui.exe", "wezterm.exe"] if platform == "win32" else ["wezterm"],
         ["WezTerm/wezterm-gui.exe", "Programs/WezTerm/wezterm-gui.exe"], ["start", "--always-new-process", "--"]),
        ("alacritty", "Alacritty", ["alacritty.exe"] if platform == "win32" else ["alacritty"],
         ["Alacritty/alacritty.exe", "Programs/Alacritty/alacritty.exe"], ["-e"]),
    ]
    if platform == "win32":
        if package_paths is None:
            packages, warning = _terminal_packages(env)
            if warning:
                warnings.append(warning)
        else:
            packages = package_paths
        specs.insert(0, ("windows_terminal", "Windows Terminal", [*packages, "wt.exe"],
                        ["Microsoft/WindowsApps/wt.exe", "WindowsTerminal/wt.exe"], ["new-tab", "--"]))
        specs.extend([
            ("conemu", "ConEmu", ["ConEmu64.exe", "ConEmu.exe"],
             ["ConEmu/ConEmu64.exe", "ConEmu/ConEmu.exe", "ConEmu/ConEmu/ConEmu64.exe",
              "cmder/vendor/conemu-maximus5/ConEmu64.exe"], ["-nosingle", "-run"]),
        ])
    elif platform.startswith("linux"):
        specs.extend([(id, label, [name], [], args) for id, label, name, args in (
            ("gnome_terminal", "GNOME Terminal", "gnome-terminal", ["--"]),
            ("konsole", "Konsole", "konsole", ["-e"]), ("xterm", "XTerm", "xterm", ["-e"]))])
    elif platform == "darwin":
        specs[0][2].append("/Applications/WezTerm.app/Contents/MacOS/wezterm")
        specs[1][2].append("/Applications/Alacritty.app/Contents/MacOS/alacritty")
    for id, label, names, locations, args in specs:
        candidates = [*names, *(str(root / location) for root in roots for location in locations)]
        candidates.extend((registered_paths or _registered_paths)([n for n in names if not Path(n).is_absolute()])
                          if platform == "win32" else [])
        found = ""
        identity = None
        for raw in dict.fromkeys(candidates):
            value = resolve_executable(raw, env, warnings)
            if not value:
                continue
            if platform != "win32":
                found = value
                break
            from backend.core.executable_identity import verify_windows_terminal
            checked = (terminal_verifier or verify_windows_terminal)(value, id)
            if checked["verified"]:
                found, identity = value, checked
                break
            warning = f"{label} rejected: {checked['code']} · {checked.get('message', '')}"
            if warning not in warnings:
                warnings.append(warning)
        if found:
            choices.append({"id": id, "label": label, "available": True, "command": found, "args": args,
                            **({"identity": identity} if identity else {})})
    if platform == "win32":
        from backend.core.executable_identity import verify_git_bash
        verifier = git_verifier or verify_git_bash
        for root in git_install_roots(env) if git_roots is None else git_roots:
            root = Path(root)
            if not resolve_executable(str(root / "git-bash.exe"), env, warnings):
                continue
            identity = verifier(root)
            if not identity["verified"]:
                warnings.append(f"Git Bash rejected: {identity['code']} · {identity.get('message', '')}")
                continue
            if identity.get("provenance"):
                build = windows_build if windows_build is not None else sys.getwindowsversion().build
                conpty = build >= 17763
                bridge = [] if conpty else [(root / "usr/bin/winpty.exe").as_posix()]
                choices.append({"id": "git_bash", "label": "Git Bash (MinTTY)", "available": True,
                    "command": str(root / "usr/bin/mintty.exe"), "identity": identity,
                    "args": ["--pcon", "on" if conpty else "off", "--hold", "error", "--exec",
                             (root / "usr/bin/bash.exe").as_posix(), "--noprofile", "--norc", "-c",
                             'exec "$@"', "gitgo", *bridge]})
                if not conpty:
                    warnings.append("Git Bash uses the verified WinPTY bridge on this Windows version; ConPTY is unavailable.")
            else:
                warnings.append("Git Bash package provenance is missing; native MinTTY is unavailable.")
            choices.append({"id": "git_bash_console", "label": "Git Bash (Windows console)", "available": True,
                "command": str(root / "git-bash.exe"), "identity": identity,
                "args": ["--no-cd", "--needs-console", "--no-hide", "--no-append-quote",
                         '--command="' + str(root / "bin/bash.exe") + '"',
                         "--noprofile", "--norc", "-c", 'exec "$@"', "gitgo"]})
            break
    selected = str(launcher.get("terminal") or "auto")
    if selected == "custom":
        command = resolve_executable(str(launcher.get("command") or ""), env)
        if platform == "win32" and command:
            warnings.append("Custom terminal identity is unverified; continuing here. Choose a verified terminal in General.")
            command = ""
        choices.append({"id": "custom", "label": "Custom terminal", "available": bool(command),
                        "command": command, "args": list(launcher.get("args") or [])})
    elif selected not in {c["id"] for c in choices}:
        choices.append({"id": selected, "label": selected.replace("_", " ").title(),
                        "available": False, "command": "", "args": []})
    preferred = next((c for c in choices if c["available"] and c["id"] not in {"auto", "current", "custom"}), choices[1])
    effective = preferred if selected == "auto" else next(c for c in choices if c["id"] == selected)
    if not effective["available"]:
        warnings.append(f"{effective['label']} is unavailable. Continue in the current terminal and choose another in /config → General.")
        effective = choices[1]
    configured = launcher.get("configured") is True or ("configured" not in launcher and selected != "auto")
    return {"platform": platform, "options": choices, "selected": selected, "configured": configured,
            "effective": effective, "warnings": warnings,
            "verification": {"reference_packages": reference_count}}
