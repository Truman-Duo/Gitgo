# Workspace search

`list_files` (find paths) and `search_text` (find content) share the same bounded
adapter and existing READ/capability/permission/isolated-runner pipeline. Both
remain composable. No new skill interface, shell command tool or network fetch
is involved.

## Query and results

`search_text` retains `pattern`, `path`, `literal`, `case_sensitive`, `include`
and `max_results`. New options:

| Option | Meaning |
|---|---|
| `output_mode` | `content` (default), `files`, or `count` of matching lines per file |
| `context_lines` | Up to five lines before/after content matches |
| `offset` | Skip sorted result rows, 0–10000 |
| `exclude` | Positive glob declarations to exclude, applied after includes |
| `include_hidden` | Explicitly allow hidden files; default false |
| `respect_ignore` | Respect local ignore files; default true |
| `timeout` | Engine deadline, 1–30 seconds (default 20) |

`list_files` uses `pattern` as a file glob and accepts `exclude`, `offset`,
`include_hidden`, `respect_ignore`, `timeout` and `max_results` (up to 5000).
Content search returns up to 2000 rows. Prefer `literal: true` for ordinary text.

```json
{
  "pattern": "ProcessStatus.RUNNING",
  "literal": true,
  "path": "backend",
  "include": ["*.py"],
  "exclude": ["**/fixtures/**"],
  "context_lines": 2,
  "max_results": 50
}
```

Compatible collections remain `matches` (`file`, `line`, `text`) and `files`
(`path`, `size`, `modified_ns`). Count mode uses `counts` (`file`, `count`).
`count` always counts returned rows, not the total matches in the whole project.
Long snippets expose `text_truncated`; use `read_file` for the complete line.

Successful engine searches report `engine`, `degraded`, `complete`, `partial`,
`warnings`, `scope`, `offset`, `truncated`, `truncation_reason` and `next_offset`.
A full page is only truncated if a further row was observed. Follow the offset
using the same query and engine; pages are repeat searches, not a repository
snapshot. Concurrent edits can change results. Beyond the page ceiling, narrow
the query instead of receiving an unusable offset.
The legacy missing-directory `list_files` response retains its exact
`exists: false` shape for compatibility; it is a missing resource fact, not engine coverage.

## Scope and bounds

Content searches skip binary files and files larger than 2MB. File discovery
does not have that size exclusion. Symlinks are not followed. Existing generated
directory exclusions (`.git`, `.gitgo`, `node_modules`, build output, etc.) still
apply to descendants of the searched root, including nested copies.

Ripgrep uses `--no-config`, `--no-ignore-parent`, `--no-ignore-global`, one worker
thread, sorted paths, bounded streaming records and concurrent stderr draining.
This avoids environment-dependent user rg configuration and unbounded captured
output. Local `.gitignore` (in Git repositories), `.ignore` and `.rgignore` retain
native ripgrep semantics. Positive include globs may override ignore rules;
explicit exclude globs, generated directory exclusions and the Host hidden-file
option take precedence. Permission-approved external roots retain their absolute
result identity. Permission is checked by the leaf pipeline before search.

The output adapter bounds records (1MB), stdout consumption (16MB), stderr (8KB),
returned row data (approximately 200KB including bounded context) and time. It
terminates and reaps the engine on deadline or early page stop. Outer isolated
runner cancellation remains responsible for terminating the whole process tree.

## Recovery and notifications

Lookup order is an absolute Host `GITGO_RIPGREP_PATH`, the frozen Host's adjacent
bundled binary, then absolute system PATH entries. Empty/relative PATH entries
and implicit workspace executable lookup are excluded. Models cannot supply a
binary path through tool arguments. The adapter never installs a dependency.

Missing/unlaunchable ripgrep enables a visible, limited Python fallback. It
supports literal, line-based search and basic `*`, `**`, `?` globs. True regex,
advanced globs and directory ignore-file semantics require ripgrep; unsupported
queries return `SEARCH_ENGINE_REQUIRED` rather than approximating them. A caller
may explicitly narrow to a file, use literal mode or disable ignore handling.
Fallback emits `SEARCH_FALLBACK`; invalid decoding or unreadable files also mark
coverage partial. A runtime engine error or invalid regex preserves the actual
failure instead of silently switching syntax.

Warnings appear in the result, durable session ledger and `ToolNotice` events.
The Agent executor maps them into the existing public `progress_summary` timeline.
Compact tool summaries expose `partial`, `more` and `fallback`. Search coverage
also appears in receipts. Transport failure keeps the retained notice; it cannot
guarantee immediate delivery to a disconnected UI. No absence claim is justified
by partial coverage, a filtered scope or an unsupported query.

Stable error identities: `GITGO-E3601` engine required, `E3602` incomplete,
`E3603` invalid arguments, `E3604` invalid regex, `E3605` engine error.

## Windows distribution

`build_windows.ps1` and `release_windows.ps1` accept `-Ripgrep` and
`-RipgrepNotices`. Builds require a standalone ripgrep >=14 and matching complete
third-party notices; no silent fallback is accepted as the release engine.
`stage_search_engine.py` stages the binary beside the frozen Host and writes a
version/hash manifest without local source paths. The package smoke gate executes
`search_text` in the frozen isolated runner and requires actual ripgrep results.
It also verifies the adjacent binary and notice hashes before execution, so a
developer's PATH cannot hide missing or mismatched packaged assets.

Source/isolated-runner tests do not replace a rebuilt frozen-package gate or the
real color-terminal Dashboard verification required by AGENTS.md. This change
does not alter terminal layout or claim those visual checks have passed.

## Real API acceptance, 2026-09-30

`scripts/real_api_search_acceptance.py` exercised the existing `gitgo` project
through Native Host → Daemon → read-only BTW Agent → isolated leaf runner with
the configured `deepseek-v4-flash` provider. Normal ripgrep and deliberately
unavailable-engine fallback cases both completed: two content/count calls and
one paged listing each, committed receipts, the real definition at
`backend/core/tools/catalog.py:20`, and `next_offset=2`. The fallback case
delivered `RIPGREP_UNAVAILABLE` and `SEARCH_FALLBACK` through the public stream;
the model explicitly reported the fallback. Credentials and reasoning are not
included in the acceptance reports. Runtime state was isolated from existing
user conversations; no source edit tool was available on this path.

This check found an existing BTW integration defect: its declared
`tool_result_open` was unavailable because storage depended on a coordination
manager that the isolated sidecar did not have. A separate Host-bound storage
dependency now serves both result opening and pipeline spill without granting
coordination authority. The sidecar also retains and emits structured task
failures instead of returning an empty failed answer. Regression tests cover
the binding, CAS opening and failure notification.

The real `run_dashboard_native.bat` also ran visibly in a new Windows cmd
console with the complete Bun/Ink tree, selected the existing project and sent
a read-only real API task through the normal UI transport. Its durable trace
contains successful ripgrep search/list receipts and `agent_complete`. This
confirms the frontend-to-backend execution route. The rendered terminal could
not be visually inspected with this session's available tools, so colors,
spacing and visible synchronization are **not** claimed as verified.

Final related regression run: **345 passed, 1 skipped**. The real symlink case
was skipped because the environment cannot create one; Windows directory
junction and scope-rejection checks passed. The installer/frozen build has not
been rebuilt or installed; its revised release gate is still awaiting a build.

## References consulted

- [Ripgrep's official filtering and glob guide](https://github.com/BurntSushi/ripgrep/blob/master/GUIDE.md)
- User-supplied Claude Code checkout: `src/utils/ripgrep.ts` (binary selection,
  bounded buffers, timeout/partial-result handling).
- User-supplied OpenCode checkout: `packages/core/src/ripgrep.ts` and leaf grep/glob
  tools (separate execution adapter, limits plus lookahead, leaf permissions).
- User-supplied Codex checkout: `scripts/codex_package/ripgrep.py` (required binary
  asset per distribution target).
