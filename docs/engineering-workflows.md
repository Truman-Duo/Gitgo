# Host-enforced engineering workflows

Gitgo does not load external skills. Engineering practices compile into a
bounded task-scoped evidence graph. The model supplies semantic choices; the
Host validates dependency order, user answers, actual tool receipts, immutable
reports and current document hashes. A report's valid structure is not proof
that its architectural judgement is correct.

## Existing mechanisms reused

- Task contracts admit workflows atomically; checkpoints preserve the graph in
  `context_snapshot`, alongside existing session state and receipts.
- `request_user_decision` supplies the existing pause/resume and question route.
- ToolPipeline enforces prerequisites even with an exact permission grant;
  composed tools check leaf operations through the same pipeline.
- `HostCompletionEvaluator` exposes missing evidence in the existing completion
  checklist. The existing explicit partial-result decision preserves failures.
- ContextObjectStore stores reports under pinned content-addressed references.
- Public `progress_summary` events use the existing Dashboard timeline. Notices
  also remain in the session Host ledger and workflow status after transport loss.

## Admission and presets

`declare_task_contract` accepts an optional `engineering_workflow` object.
Alternatively, call the `engineering_workflow` tool with operation `configure`.
These operations grant no additional tool or filesystem authority.

Omitting `nodes` compiles a built-in practice preset:

```json
{
  "profiles": ["diagnosis", "tdd", "retrospective"],
  "test_id": "resize-regression",
  "target_files": ["src/renderer.ts"],
  "preparation_files": ["tests/resize.test.ts"],
  "check_argv": ["bun", "test", "tests/resize.test.ts"],
  "check_cwd": "."
}
```

Without `check_argv`, checks use the registered `run_test` protocol. Exact argv
checks support other test runners; only completed exit 0/1 results count as
green/red. Timeout, launch error and cancellation are not a reproduced failure.
Command receipts bind argv and cwd. This is evidence bookkeeping, not a sandbox
or a guarantee that a test checks the right symptom.
Red and green command nodes must bind the same argv and cwd; sharing only a
test label cannot substitute a different passing command.

Presets:

| Profile | Compiled Host checks |
|---|---|
| alignment | Questions with prerequisite edges, or a structured alignment report |
| domain_modeling | Hash a concrete glossary (`glossary_path`, default `GLOSSARY.md`) |
| diagnosis | Red evidence → falsifiable hypotheses → green evidence → cleanup report |
| tdd | Red evidence → target-file change receipt → green evidence |
| architecture | Inspection receipt → alternatives and public test-surface report |
| retrospective | Findings derived from actual current-task failed receipts |
| agent_documentation | Concrete `agent_document_path`, local-link validation and navigation report |

Alignment questions supply `id`, `state_topic` and optional `depends_on`.
Independent questions form one ready frontier. The existing UI presents one
decision card at a time; later dependent questions cannot be asked early.
No extra user confirmation is manufactured for facts that the Host can inspect.

`preparation_files` permits authoring an evidence harness before red. It cannot
overlap declared TDD product targets. Ordinary product edits still wait for
prerequisites. Commands are only exempt at a ready, exact declared check node.
Underlying capability and permission checks always remain active.

## Custom graphs

Supply `profiles` and `nodes` for explicit steps. Each node has a stable `id`,
`kind`, `depends_on`, and optionally `before_mutation`. Selected practices keep
their core evidence mandatory. Cycles, missing dependencies, outside paths and
contradictory red/green order are rejected before publication.

Evidence kinds are `decision(state_topic)`, `observation(tools)`,
`check(test_id,passed)`, `change(files)`, `document(path)` and `report(format)`.
Check nodes can additionally bind `tool_name: exec_command`, `argv` and `cwd`.
Use a green node as a dependency of the next slice's red node for vertical TDD.

The tool operations are:

- `status`: current frontier, evidence references and retained notices.
- `ask`: `node_id` plus a normal structured decision `request`.
- `record`: a document node or a structured report in `content`. Actual receipts
  and user answers are observed by Host, never supplied as model assertions.
- `retrospective`: deterministic findings from this task's receipts.
- `propose_amendment`: a validated replacement `plan` and user-visible `reason`.
  The existing question card shows before/after requirements. Only the matching
  explicit user choice activates the replacement; free-form discussion keeps
  the original requirements and produces a notice.

Reports use these required fields:

| Format | Fields |
|---|---|
| alignment | `decisions`, empty `unresolved` |
| hypotheses | `symptom`, 2–5 `hypotheses`, each with `prediction` and `probe` |
| architecture | `candidates`, each with `files`, `problem`, `test_surface`, ≥2 `alternatives` |
| cleanup | `removed_instrumentation`, `remaining_limitations` |
| retrospective | `findings` (automatically generated when content is omitted) |
| agent_documentation | `triggers`, `completion_criteria`, `references` |

Document writes use normal file tools. Recording observes the resulting file;
it does not rewrite documents or run hidden shell commands.

## Recovery and limitations

Active plans evolve additively. Requirements cannot be silently deleted or
weakened; real scope changes use `propose_amendment` through the existing user
decision broker. The existing completion-exception decision can also accept an
explicitly incomplete result, preserving missing facts. Missing CAS reports and changed documents invalidate dependent
evidence and produce notices. Receipts before admission or before prerequisite
evidence cannot satisfy a later stage. Another task does not inherit the graph.
Target-file changes aggregate multiple committed receipts. Subsequent writes
invalidate old green evidence and require a fresh check. Green evidence is
conservative across workspace writes, including evidence-harness changes.
Composite leaf calls still require prerequisites already committed by Host;
a composite cannot use a not-yet-published sibling result to waive a gate.

Unavailable verification remains missing. Report storage failure returns
`GITGO-E6201`; unmet execution prerequisites use `GITGO-E6202`. Neither becomes
successful verification. Event subscriber failures are retained, logged and
notified while independent consumers continue.

This feature does not replace the permission broker, add OS sandboxing, execute
third-party skill instructions, automatically delegate, or verify formal terminal
appearance through a simulated renderer. Formal Dashboard visual checks continue
to follow AGENTS.md.
