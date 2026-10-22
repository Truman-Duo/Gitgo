# Native execution isolation

Use the current tool capability snapshot and returned diagnostic. Isolation is enforced by the runtime; approval does not disable it.

- Unavailable backend, invalid policy or denied launch: no new execution was authorized by that failure. Inspect the native runtime configuration; do not retry outside isolation.
- Unsupported shell: Windows currently supports PowerShell, Linux supports Bash. Use the supported syntax for a new call; do not silently translate an approved script. Windows Git Bash support is tracked separately.
- Output or resource limit: inspect retained evidence and reduce the workload or output before a new bounded call. A terminated process may already have changed its execution workspace.
- Cleanup unconfirmed: inspect the recorded runtime debt. Cancellation, timeout, an exit code or a new approval does not prove the process tree has been cleaned up.
- Execution/evidence failure: distinguish business effects, launch, evidence persistence and cleanup. Keep unknown effects unknown; do not replay a potentially committed effect to obtain a cleaner receipt.

- Protected Git unavailable/config unsupported: inspect the exact catalog diagnostic through the existing guidance interface. It describes this refused Git command, not a rollback of earlier task effects. Do not disable the protection, switch to shared execution, edit repository config to clear the check, or replay prior promotion effects. An operator may need to repair the supported runtime/configuration.
- Custom Git filters are disabled in trusted worktree plumbing. If Git reports a required clean/smudge/process filter failure, inspect the retained worktree and report the unsupported transformation. Do not stage unfiltered bytes or claim that arbitrary filter code is safe because Git initiated it.

These instructions explain existing capabilities. They grant no authority and do not change the task, project, file version or approval scope.
