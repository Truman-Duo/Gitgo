# Usability work: original scope and verified progress

This checklist preserves the user's original 1–4 and subsequent discussions.
A completed subtask does not close the parent item. Source tests are distinct
from a rebuilt distribution or formal Dashboard acceptance.

| Original item | Current progress | Still required |
|---|---|---|
| 1: Find/read/write and common harness tools | Existing versioned read/write tools retained. File/content search now shares the bounded ripgrep adapter, explicit fallback and notices. | Complete inventory and common text-tool assessment; complete read/write usability audit. |
| 1: Terminal execution | argv execution versus explicitly approved Bash audited. Windows Bash shares publisher/signature/behavior/hash verification; startup injection variables removed. | Broader shell quoting/encoding/output-bounds/OS audit; POSIX verification. |
| 1: Questions, permission and engineering practices | Seven task-admitted practice presets use Host dependency/evidence checks, existing decisions, permissions, CAS and checkpoints. | Broader governance/invention audit; this does not automatically apply every practice to every task. |
| 2: Multiple providers | Not implemented in these usability changes. | Provider configuration/routing, circuit-breaker fallback, local-model integration; consult the supplied cc-switch checkout. |
| 3: Bun/Node and operation modes | Not implemented in these usability changes. | Reproduce Shift+Tab, compare updated Bun and Node, decide runtime from evidence; define auto/manual/plan/accept semantics and switching. |
| 4a: Windows 10 cmd resize crash | Terminal inventory, first-run General selection, arrow-key changes, verified Windows Terminal and native Git Bash/console recipes and explicit fallback implemented. Native MinTTY and DLLs match an official package reference; visible terminal acceptance remains pending. | Reproduce and fix original resize crash; verify formal visible UI and launcher, exercise older-Windows WinPTY fallback on that OS, rebuild installer. |
| 4b: Project list running status | Not fixed by these changes. | Repair the state projection and verify navigation while a project is running. |
| Follow-up: Self-assembled tools and signals | Existing leaf pipeline reused for search and practice gates; EventBus subscriber failures and tool warnings produce retained/public notices. | Audit composed and invented tools invoking privileged capabilities; evaluate a unified Runtime/Host → Events → State/UI/CAS protocol, dependent signals and recovery. |
| Follow-up: Governance usability measurement | Passive Daemon collector derives bounded metrics from persisted trace/CAS, with missing-data diagnostics and offline summaries. | Accumulate normal usage; evaluate delivery purpose and prompt/state design from evidence, without claiming causality from one trace. |
| Follow-up: Collaborative Git workflow | Multi-repository model and contributor/PR conventions documented. | Implement candidate identity, explicit formal targets, downstream synchronization, concurrent acceptance and lifecycle projections; current docs are design rather than an implemented distributed workflow. |

## Completed source subtasks

- [Engineering practice infrastructure](engineering-workflows.md): task-scoped
  graphs, real receipts/answers, completion gates, durable recovery and explicit
  scope amendments. Semantic/architectural judgement remains model work.
- [Search adapter](search-tools.md): real ripgrep, file discovery/content/files/
  counts, scoped filtering, sorted pages/context, bounded pipe handling, explicit
  degraded recovery, notices and permission-preserving isolation/composition.

Search packaging now stages an operator-supplied binary and matching notices,
records hashes and requires frozen-runner search in the release smoke gate.
The new installer has not been rebuilt/installed as part of source validation.
Normal/fallback real API acceptance passed on the existing Gitgo project. The
formal Dashboard was launched visibly and its real search execution was confirmed
from durable receipts; rendered-terminal visual acceptance is still outstanding.
Real API verification also exposed and repaired isolated BTW storage binding and
empty failure reporting. The OS-specific
real symlink test can be skipped where the environment cannot create a symlink;
scope rejection and Windows directory-junction checks are separate tests.

The next bounded work item after search is the terminal/Shell audit and repair.
[Terminal source progress and acceptance limitations](terminal-launcher.md) are
recorded separately. Process/API success is not a visible-window pass; the
native Git Bash option now uses verified MinTTY with ConPTY (or WinPTY on older
Windows). Real installation checks verified all 79 package images, automatic
Host-owned preference filling, substituted-MinTTY rejection and unchanged global
config. Native window automation rejected Git Bash startup, so the user-visible
formal acceptance remains open. The repeatable manual entry is now
`test_terminal_selection.bat`: each click opens the formal first-run selector,
saving opens the chosen terminal, and a detached lifetime keeper removes the
unique temporary profile after its dashboards exit. Five Windows lifecycle
checks passed, including real Bun handoff and abrupt close. This does not
replace human inspection of the visible terminal. The older dated acceptance
runners are historical fixtures.
Provider, runtime/mode design and the two frontend defects remain on this list.

## Current closure and public handoff

The initial search and terminal acceptance notes above describe their dated
validation stages. Subsequent complete Bun/frozen-Host bundles passed actual
local HTTP Provider continuity, shared-history ownership and automatic
statistics gates. The user confirmed the source Ctrl+G fix. A new installer
and full visible rendering acceptance for every terminal/OS have not been
claimed. Normal configuration changes defer terminal selection until the next
launch; terminal origin is stored separately from shared session identity.

For the current candidate, scope, compatibility overlaps, verification and
remaining work are collected in [the handoff](usability-baseline-handoff.md).
