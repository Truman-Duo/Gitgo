# Bundled Harness prompts

These are runtime product resources. `backend.core.prompt_templates` loads them
from the installed backend package, independently of the working directory.

| Directory | Text responsibility |
|---|---|
| `identity` | Main process, worker, reviewer and direct-answer identities |
| `policy` | Stable behavior, collaboration and agent relationship guidance |
| `completion` / `contracts` | Delivery instructions and task-scoped wording |
| `routing` | Guidance for the route selected by runtime code |
| `runtime` | Current-turn guidance, capability snapshot, provider change and BTW |
| `alignment` | Semantic requirement and delivery assessment |
| `context` | Compaction, coordination and current evidence explanations |
| `recovery` | Stream, incomplete output, malformed calls and checkpoint recovery |
| `knowledge` | Lesson harvesting and retrieved-lesson synthesis |
| `tools` | Protocol and workspace tool usage; parameter schemas stay in code |
| `guidance/*.md` | On-demand handling procedures with Markdown sections and selective boundary examples |
| `legacy` | Compatibility prompt entry points |

Runtime prompt files contain only model-visible text: no front matter or hidden comments.
This README is maintenance documentation and is not a runtime prompt. Long handling
procedures use explicit `.md` resource identifiers; extensionless identifiers select
`.txt`. Loading never probes another extension. `profiles.json` remains structured
configuration, and tool schemas/error definitions remain in their owning code.
`${name}` declares a dynamic slot; `$$` represents a literal dollar sign. JSON
braces are literal. Substitution happens once, without evaluating expressions or
recursively expanding inserted data. Missing or unexpected variables are errors.
Whitespace is significant; adding a final newline changes the rendered prompt.

Runtime code chooses the appropriate resource and supplies observed facts. The
resources do not grant permissions, implement state transitions or validate tool
arguments. Project-local files cannot override them. The existing user configuration
selects a release-owned semantic instance; it does not supply arbitrary paths
or resource replacements.

Prompt changes are versioned through Git and the existing compiler content
hashes. Already-sent prefixes retain their original bytes; the compiler projects
later changes using its existing steering mechanism. Restart the owning process
after editing resources, since templates are cached for its lifetime.

Semantic changes to `alignment/assessor.txt` also require updating the existing
`alignment_routing.POLICY_VERSION`: assessment reuse binds that version, not the
compiler's system-prompt hash. Keep response formats consistent with their code
validators when changing any template.

The Windows build includes this directory as backend package data. Other build
paths must retain the same package-relative resource layout.

## Semantic instances

Stable files remain the resource entry points. Nine of them contain eleven
reserved `${_instance_*}` slots for presentation and method guidance.
`profiles.json` declares these slots and selectable identities;
`profiles/<id>/*.txt` stores one complete set. All other text remains in the
stable shells, including identity facts, authorization, completion/tool
protocols, harvest classification/qualification and dynamic fact slots.

`default` (Baseline) retains the original instance wording; `workbench`
(Workbench) contains reviewed alternatives. Both share the current stable
core. Comparing a complete historical prompt also requires the matching core
version. Workbench is not a claim of validated behavioral improvement. In Config → General → Prompt Instance, choose a set and save.
The existing `config.set` operation accepts `prompt_profile`; invalid or
incomplete choices are rejected before persistence.

A running agent execution pins one instance; new executions read the saved
selection. Already running tasks do not mix instances when the setting changes.
Existing sessions receive actual text changes through normal prompt steering.
Resources remain cached: selecting an existing instance needs no restart;
editing its files requires restarting the owning process.

To add a set, copy the complete fragment directory and add its id, label and
description to the bundled manifest. The configuration selector reads its
choices from that manifest. Adding or changing a reserved slot is a contract
change, not merely adding an instance. Missing/extra files, unknown identities
and invalid resources fail explicitly; there is no inherited fallback.
Caller-supplied values cannot fill reserved slots, and inserted fragment text
and observed data are never recursively interpreted.

Stable text is still maintained with the release. Its presence does not prove
arbitrary instance wording is semantically compatible; review tuned instances
and evaluate actual tasks before making them the baseline.


## Tool usage guidance

Keep interface-specific usage guidance in the corresponding `tools` resource,
loaded by its existing registration. Names and parameter schemas remain in the
registered AgentTool and are serialized from it for the model. Semantic instance
fragments should describe the decision to seek evidence without duplicating API
call syntax. A changed interface still needs its usage conditions reviewed.

The answer delivery instance is shared by the behavior resource across task
routes. Completion adapters share the result-field description; they retain their
own validators and completion semantics. Prompt wording does not implement
checkpoint restoration, permission, or evidence capture.

Review method is an instance fragment; reviewer authority, read-only responsibility
and completion protocol remain in stable resources. Workspace tool guidance loads
from its canonical registration, including platform-specific shell guidance.
Source-authority rules are shared; tool-use/feedback guidance is mounted when
tools are available, and implementation guidance with the corresponding workspace
capabilities. These describe existing boundaries and do not add enforcement.
