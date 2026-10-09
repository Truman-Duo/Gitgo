# Terminal discovery, preferences and identity verification

Status as of 2026-10-08: compiled Bun frontend and frozen Host bundle built at
`dist-terminal/20261008-continuity`. Packaged protocol continuity and exclusive
ownership checks passed. The user confirmed the source Ctrl+G repair works;
this does not certify visible rendering of the newly packaged terminal flow.
No preview installer was compiled or installed for this closure.

## User flow

The Native Host supplies `config.terminals`; the frontend never asks the user
to find or enter an executable path. First launch opens `/config → General`
on the Terminal row. Left/right selects an available choice, Enter saves, R
rescans. Later launches use the same row and changing it applies on the next
launch. The first selection can hand off immediately to the chosen terminal.
Esc cancels the form; an unanswered first-run preference is asked again next
launch. No unreviewed provider task starts while first-run setup is pending.

The saved preference contains a terminal ID and `configured` marker. Host
discovery derives command/arguments/identity. Saving rescans so a removed or
unverified terminal cannot be persisted as an available choice. Explicit
legacy preferences retain their meaning; old `auto` preferences without the
marker receive the initial choice. Missing preferences produce a warning and
retain the current terminal. Config errors and detection errors are displayed.

Source mode preserves the Bun entry point before Gitgo flags. Packaged launches
from Explorer or an existing shell use the preference; explicit `--attached` invocations stay
attached. A one-use expiring handoff ticket transfers only an allowlist of
runtime/profile/color settings. The parent stays alive until the child starts
the real Host and full renderer and acknowledges readiness. Launcher exit(0)
alone is insufficient. Missing executables, failed starts and timeout retain
the parent and explain how to recover in General.

## Shared state and deferred preference (2026-10-08)

PowerShell, Windows Terminal and Git Bash resolve the same user config and
project SQLite/CAS store. Terminal preference never enters the project identity
or storage path. `GITGO_STATE_HOME` remains an explicit isolation override;
`test_terminal_selection.bat` intentionally uses a disposable empty store and
deletes it after closing, so that BAT is not a cross-launch history test.

The frontend forwards bounded `GITGO_FRONTEND_ORIGIN` metadata. The Host captures
it once and persists it in runtime preferences and task-admission trace details.
Changing next-launch preference cannot relabel the current task origin. This is
diagnostic frontend provenance, not authenticated authority. A plain Windows
console is recorded as `windows_console`, rather than guessing its shell.

Each data profile has a stable `dashboard-owner.lock`. An OS-held exclusive lock
rejects a second new-version Host with `HOST_PROFILE_IN_USE`; it releases on normal
exit or hard termination. It is not a promise to coordinate unrelated legacy
versions, MCP writers, or intentionally separate data profiles. Lock errors are
reported explicitly. The lock file is never deleted or replaced.
An interactive compiled dashboard keeps startup errors visible until Enter is
pressed, so an Explorer-created error window cannot disappear immediately.
Noninteractive/CI startup failures still terminate immediately with a nonzero code.

Only the initial terminal-selection overlay may hand off immediately. Dismissing
that overlay also ends the initial handoff opportunity. All subsequent `/config`
saves display “applies on the next launch” and leave the current renderer/Host
running. Next ordinary packaged invocation uses the saved terminal; `--attached`
is the explicit already-attached child path. First-time handoff closes and waits
for the original Host before spawning its successor, and restarts the original
Host if the handoff fails.

The build checks core Host/Daemon imports before compilation and runs the
terminal/input/renderer tests plus `scripts/check_terminal_continuity.py` on the
actual frozen Host. The latter uses an isolated local HTTP Provider: first turn,
save preference, another turn in the same Host, close, restore via a second Host,
third turn. It checks one store/session, all six user/assistant messages, history
in the next provider request, original/next origin metadata, and rejection of a
simultaneous Host. It does not automate or certify a visible terminal.

### Visible packaged acceptance procedure

1. Close other Gitgo dashboards using the normal profile. Open the generated
   `dist-terminal/20261008-continuity/gitgo.exe` with its complete sibling
   `internal` directory in place. Do not copy only the exe. Do not use the
   disposable BAT for this test.
2. Enter the same existing project; send a distinctive message and wait for its
   completed answer. In `/config → General → Terminal`, choose the automatically
   discovered Git Bash using left/right and save. The current window must remain
   open, and the next-launch notice must appear.
3. Send another message before closing. It must stay in the current terminal.
   While that window is open, a second ordinary invocation must report that the
   data profile is already in use and must not start another dashboard writer.
4. Close Gitgo, then invoke the same packaged exe normally. Git Bash should open
   and the same project should show both preceding turns. Ask a follow-up that
   depends on those turns. Repeat the preference/save/close/reopen steps for the
   other available terminal option. Windows Terminal uses its installed default
   shell profile; selecting a terminal does not configure the Agent's exec shell.
5. Check colors, borders, cursor placement and Ctrl+G edit/submit without resizing
   the terminal. This is visible UI acceptance; protocol checks cannot replace it.
   No user history should be cleared during these steps.

After the user reported no visible window, a missing readiness condition was
found: a child must also have interactive stdin and stdout before acknowledging
the handoff. This condition is now enforced and tested. TTY presence alone is
still insufficient to certify on-screen appearance: visible inspection remains
a separate acceptance step.

## Discovery and trust

Windows discovery checks absolute PATH entries, App Paths registrations,
Windows Terminal packages, Git for Windows installation registrations and
known install directories. It excludes cwd, empty and relative PATH entries;
it does not execute candidate programs just because their names match.
Windows verifier tools are resolved from the system directory returned by
Windows rather than a candidate-provided PATH/SystemRoot.

Windows Terminal requires a valid Microsoft Authenticode signature and the
expected original-image name. Execution aliases are resolved to the package
image. A renamed Microsoft-signed `cmd.exe` does not qualify. Other Windows
terminal recipes have no approved publisher policy yet and are explicitly
reported as unverified rather than launched. Legacy custom commands similarly
fall back with a warning on Windows. Adding a terminal requires a publisher
policy and launch recipe, not another filename-only heuristic.

Git Bash requires valid Git for Windows publisher signatures on `git-bash.exe`,
`bin/bash.exe` and `usr/bin/bash.exe`, matching signer identity, confined paths,
a bounded behavior check with a nonce, and unchanged image hashes. Hashes are
checked again at launch. Failed/unsigned/incorrect publishers are rejected
before candidate execution. A failed explicit Bash override cannot silently
fall through to another binary or WSL.

The actual installation on this machine was automatically found at
`C:\Program Files\Git`. Its launcher and both Bash images have valid signatures
from Johannes Schindelin; the engine reports 5.2.37(1)-release. In contrast,
the bare PATH `bash.exe` points to the WSL launcher and is never treated as
Git Bash.

**The installed MinTTY is unsigned.** It is now verified by package provenance,
alongside the signed launcher/Bash anchors. `stage_terminal_provenance.py`
downloads an official Git release archive, checks its published SHA256, and
hashes images without extracting or executing them. Only inert references ship
with the Host; discovery does not download programs or trust manifests supplied
by a candidate or user launcher configuration.

`package_provenance.py` matches all signed anchors to one bundled release and
requires every referenced terminal image/DLL to match it before the Bash probe.
Additional DLLs in the root, bin or usr/bin directories are rejected. Missing
dependencies cannot fall through to PATH. Hashes and directory inventories are
checked again before launch; the Agent's Bash tool uses the same policy.
This does not claim atomic protection from privileged replacement between the
final check and process creation.

The native option is **Git Bash (MinTTY)**. On Windows build 17763 or later,
it enables ConPTY explicitly so native Bun receives interactive console handles.
Older versions use the verified WinPTY bridge and report the fallback. The
separate **Git Bash (Windows console)** option retains its existing recipe.
Both use bounded handoff readiness and visible failure notices; a failed native
launch retains the current window rather than quietly switching the selection.
MinTTY holds its window on command failure to keep diagnostics available.
The old-Windows WinPTY route has automated recipe coverage, not OS acceptance.

The initial shipped reference covers Git 2.53.0.windows.2 x64. Unknown or altered
installations are explicitly reported as unverified. Supporting another Git
release means staging another official reference through the same build script,
not adding an installation path or relaxing the publisher policy. Windows
packaging requires references and includes them in the frozen Host; its smoke
gate requires the frozen `config.terminals` operation to load them.

The Linux/macOS discovery recipes are implemented but not tested on those OSes.
They do not claim Windows-style publisher verification.

## Agent shell tools

A terminal hosts the dashboard; it does not choose the Agent's command shell.
`exec_command` executes an argument vector, with no implicit OS shell.
`shell_script` remains an explicitly approved Bash program and now shares the
Windows Git/Bash identity checks. Validation failure returns the catalogued
`BASH_IDENTITY_UNVERIFIED` error and does not start the script. Its environment
strips credentials, startup injection variables and exported Bash functions.
Exact invocation approvals, process isolation, cwd confinement, timeout and
existing process-tree cleanup remain active.

A related regression exposed engineering checks taking precedence over invalid
tool arguments. Prerequisites now run after prepared-argument validation and
before consuming a one-use grant. Valid approval still cannot waive missing
engineering evidence.

## Evidence and remaining acceptance

- 25 frontend/router/transport/launcher tests passed; TypeScript checking passed.
- 184 Python terminal, Host, development runtime, permissions, recovery, engineering
  and preflight regression checks passed (183 together plus one focused grant
  consumption check).
- A real counterfeit fixture copied Microsoft-signed `cmd.exe` images under Git
  launcher/Bash names. Actual Windows signature verification rejected the
  unexpected publisher, and the probe/script was never executed.
- An isolated initial config contained `auto`, an empty command and no saved
  terminal identity. Full frontend key events chose the discovered ID; the
  normal Host save operation populated command/arguments/verified identity.
- The Git launcher → Bash wrapper → Bash engine → Bun dashboard → Python Host
  process chain was observed. An existing Gitgo project completed a real
  DeepSeek API task with one successful, committed, complete ripgrep search.
  The user's global configuration hash stayed unchanged.
- That process/API run predates the strengthened interactive readiness condition.
  The user did not see its terminal window, so it is **not** visible UI acceptance.
  The native window tool then returned `product policy blocks this app` for
  `git-bash.exe`. No blocked-tool workaround was attempted.

Current source checks (2026-10-07): 191 Python checks passed with one environment
skip; the focused terminal/provenance suite passed 20 checks after adding the
missing-DLL regression. The frontend suite now passes 26 checks and TypeScript
checking passes. Real Host discovery outside the sandbox found Windows Terminal,
Git Bash (MinTTY), and Git Bash (Windows console), with no warnings. Its isolated
config initially held only `auto` and an empty command. Saving the discovered ID
through the real Host populated the MinTTY command and verified identity.

All 79 images in the installed Git package matched the official reference.
A copied real package retaining its genuine launcher/Bash images but replacing
MinTTY with a Microsoft-signed unrelated executable was rejected as
`TERMINAL_PACKAGE_UNVERIFIED` before any behavior probe. The original global
configuration hash remained unchanged. Reproducible checks and results:
`scripts/check_terminal_installation.py` and
`.gitgo/terminal-provenance-check-20261007/result.json`.

The retained preparation and result records live under
`.gitgo/terminal-acceptance-20260930-1/`. `scripts/terminal_acceptance.py` prepares
future isolated first-run profiles without filling discovered launcher paths;
its generated `run.ps1` starts `run_dashboard_native.bat` and automates only
normal input. Provider credentials remain in their existing user-scoped store.

The updated isolated formal acceptance entry is
`.gitgo/terminal-acceptance-20261007-1/run.ps1`. Running it manually in a real
color-capable PowerShell terminal starts the formal `run_dashboard_native.bat`,
chooses the discovered native Git Bash ID through normal input events, then
hands off the existing Gitgo project and the bounded DeepSeek search task.
It has been prepared but has **not** been run or visually accepted. The test
keeps `visible_window_verified=false` until a human inspects the window; a
successful backend task cannot change that field.

Before closing this work item: inspect the visible formal General selector,
colors, selection, notices and bottom bars; confirm the chosen terminal window
appears and continues the existing project; test later preference changes and
failure recovery on screen. The original Windows 10 cmd resize crash and the
new installer are still outstanding. The native UI tool previously blocked
Git Bash startup; its current computer-use guidance also prohibits terminal
automation. This is a tool-policy limit, not a request for renewed user approval.

## Repeatable human terminal selection (2026-10-08)

Double-click **`test_terminal_selection.bat`** in the repository root. This is
the supported repeatable manual entry; the dated `run.ps1` files above are
retained historical fixtures, not the procedure to repeat.

1. The BAT opens `run_dashboard_native.bat` with the complete formal Bun/Ink
   frontend and native Host. Its first-run overlay focuses `/config` → General
   → Terminal. No timed input is injected.
2. Use **Left/Right** to choose **Git Bash (MinTTY)**, then **Enter** to save.
   Automatic Host discovery and verification supply executable paths; the test
   does not write a Git installation path or preselect an option. Saving opens
   Gitgo in the chosen terminal and closes the original dashboard after the
   chosen dashboard acknowledges interactive renderer readiness. `Current
   terminal` deliberately continues in the original window.
3. Inspect the real selected window: colors, selection, normal and command
   bars, bottom status, project list, and resizing. A backend process or API
   result alone does not confirm these. No API task is submitted automatically.
4. Close the test Gitgo normally or close its terminal window. A detached
   cleanup keeper waits for the coordinator and every registered dashboard
   process to exit, then removes this run's configuration, provider copies and
   runtime state. It tracks Windows process handles and creation times, so an
   abrupt exit does not depend on an exit callback or terminal launcher PID.
5. Double-click the BAT again: the selector starts fresh, even if another test
   is still open. Every run gets a unique UUID directory under
   `.gitgo/terminal-tests/`; there is no fixed directory to trigger WinError 183.

The launcher reads the original config explicitly to avoid legacy migration,
resets only the temporary launcher preference, and copies provider metadata and
encrypted secrets into the temporary profile. UI edits during the test affect
these copies. Existing project paths are retained; ordinary project actions
still operate on those real projects. Close the test instead of submitting
work if testing only terminal selection.

The one-use terminal handoff carries the temporary lifetime environment along
with the existing isolated config/state paths, including when Windows Terminal
uses an already running server. The child registers **before** acknowledging
readiness. The source dashboard exiting during handoff therefore cannot erase
the selected dashboard's state. Normal launches without a lifetime environment
retain their existing behavior.

`.gitgo/terminal-tests/last-result.json` is a small lifecycle diagnostic, not a
saved test profile. `cleaned=true` means the owned directory was removed; its
`selected_terminal` records the saved preference and **does not prove visual
acceptance**. Cleanup retries transient file locks for up to 20 seconds. On
failure, `run-<id>.cleanup-error.json` retains the reason and the next BAT prints
its location. A keeper startup failure is reported in the initial console,
and a keeper failure while Gitgo runs is reported by the frontend. Cleanup
refuses foreign roots, redirected directories and mismatched identities.

Validation: five Python lifecycle checks use real Windows process handles and
real Bun processes, including handoff followed by abrupt termination. They
also cover repeated preparation, user-file isolation, owned deletion and lock
retries. Frontend registration/handoff and General input tests pass; TypeScript
checking passes. These are **process and code checks**, not visible terminal
QA. The native computer-use policy blocks terminal automation, so human
inspection through this BAT remains the final visible check.

## Primary references

- [Windows Terminal CLI](https://learn.microsoft.com/en-us/windows/terminal/command-line-arguments)
- [Git wrapper behavior](https://gitforwindows.org/git-wrapper.html)
- [Git for Windows wrapper source](https://github.com/git-for-windows/MINGW-packages/blob/main/mingw-w64-git/git-wrapper.c)
- [WezTerm start](https://wezterm.org/cli/start.html)
- [Alacritty CLI](https://alacritty.org/cmd-alacritty.html)
- [ConEmu arguments](https://conemu.github.io/en/ConEmuArgs.html)
- [MinTTY directory, hold and ConPTY options](https://mintty.github.io/mintty.1.html)
- [Official Git reference release and SHA256](https://github.com/git-for-windows/git/releases/tag/v2.53.0.windows.2)
- [WinPTY architecture and native-program bridge](https://github.com/rprichard/winpty/blob/master/README.md)
