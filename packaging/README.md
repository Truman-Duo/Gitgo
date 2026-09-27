# Terminal release boundary

Gitgo v1 is one terminal product with one public command. The installed
dashboard may start an internal Native Host process, but the Host is not a
second user-facing application and is versioned atomically with the dashboard.

The installer adds its installation directory to the current user's `PATH`, so
`gitgo` works in an existing terminal. A shortcut/double-click launcher opens
the terminal selected in the user configuration; an attached TTY or SSH
session is always reused. The final public command and optional aliases come
from `product.json`, so packaging does not freeze the eventual product name in
runtime code.

The launcher reads `~/.gitgo/config.json` before starting the Host. Its stable
JSON shape is:

```json
{
  "launcher": {
    "terminal": "auto",
    "command": "",
    "args": []
  }
}
```

`terminal` accepts `auto`, `current`, `windows_terminal`, or `custom`. `auto`
prefers Windows Terminal only for Explorer/shortcut launches. `current` always
keeps the console Windows assigned to the executable. A custom terminal runs
`command` with `args`, followed by `gitgo.exe --attached`; for example WezTerm
can use `{"terminal":"custom","command":"wezterm.exe","args":["start","--"]}`.
Changing this file is picked up by a running Host for runtime settings; launcher
selection naturally applies on the next product launch.

The Windows installer also registers the primary executable under the current
user's `App Paths`. PATH removal is segment-based and removes only the exact
installation directory; it never restores an old whole-PATH snapshot over
changes made by other applications. Inno Setup supplies the dedicated
uninstaller. `build_windows.ps1 -BuildInstaller` creates it when an Inno Setup
compiler is available; staging the product does not silently install one.

The uninstaller owns only the installation and user configuration/credential
files. Runtime/session databases are retained for recovery. It must never
traverse configured workspace paths, remove repositories, delete worktrees, or
remove a project's `.gitgo`/`.git/gitgo` directory. Automatic update is
intentionally out of scope for the first installer.

Platform packages are built independently. Windows uses an `.exe` launcher and
installer; macOS and Linux expose the same command/protocol/config contracts but
use native package and credential-store adapters.

## Release build prerequisites

`build_windows.ps1` selects `GITGO_PYTHON`, `~/.gitgo/runtime/python`, or the
per-user LocalAppData Gitgo runtime, in that order. The selected build runtime
must pass Gitgo's SQLite WAL-safety guard. PyInstaller may be installed in that
runtime or supplied as a separate build-only package directory with
`-PyInstallerPackages`; the script also recognizes the runtime's inactive
`packages/` build directory. It fails closed instead of falling back to a
system Python with an unsafe SQLite build. Bun is resolved from
`~/.bun/bun.exe` unless passed explicitly.

The compiled Native Host is smoke-tested through the versioned stdio protocol
before publication. Release artifacts remain under ignored `dist-terminal/`
and `dist-installer/` directories; executables and runtime databases are never
committed to Git.

The repository-root `build.py` is retained only to archive the early Qt Git
manager. It requires an explicit `--legacy-qt` flag and is not part of the
terminal release pipeline.

## One-command release verification

Run `packaging/release_windows.ps1` from PowerShell for the maintained release
flow. It fails closed on a dirty tree, invalid commit messages, an unsafe
SQLite runtime, tracked secrets/private state, Python or Dashboard test
failures, and build failures. By default it then stages the Windows product;
`-VerifyOnly` performs every check without packaging, while `-BuildInstaller`
also invokes Inno Setup when its compiler is already installed.

The script never pushes, installs, rewrites Git history, downloads build tools,
or deletes project/runtime data. `-AllowDirty` exists only for a local rehearsal
and should not be used as release evidence.
