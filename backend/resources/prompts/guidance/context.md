# Context

## Recover bounded output

Use valid current tool-result locators to fetch a bounded missing window through context_open/context_search. Do not rerun a mutation to recover truncated output. A locator is bound to the task lineage; do not invent one or copy another task's reference.

## Follow runtime compaction

Context-limit and compaction failures are handled by the existing context runtime with bounded attempts and, when required, a real user decision for destructive compaction. Do not imitate those approvals or call a fictitious compact tool.

## Continue from current evidence

After recovery, use reprojected task facts and the current capability snapshot; a summary is lossy and cannot replace authoritative receipts or the latest confirmed requirements. If the runtime cannot recover, report the concrete limit and preserve safe state.

## Truncation example

- Appropriate: a mutation already returned a committed receipt and a current output locator; fetch the needed output window.
- Inappropriate: rerun the mutation because its visible output was truncated.
