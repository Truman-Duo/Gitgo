# Custom tools

## Read the current catalog

Inspect the current saved-tool catalog and concrete validation details. Correct a definition or select an existing version through registered tool APIs.

## Preserve registration authority

Immutable version digests, content privacy checks, leaf capabilities and source-bound privileged approval remain authoritative. Editing a prompt or wrapper does not approve privileged code or grant a missing capability.

## Version and storage recovery

Resolve a name collision intentionally; do not overwrite another tool by default. If a saved version fails integrity checks, stop using that version and inspect storage. Catalog/observer recovery must not replay a previously committed external effect.


## Private native outputs

An approved privileged program runs in a private business-file view. Its data is returned under tool_output; formal files, candidate references, lesson checks and definition facts come from the runtime/file service. The same view limits and relative-path requirements as exec_command apply. candidate_ref and lesson_feedback_id are reserved continuation parameters for these tools; a legacy definition using those as business inputs must be revised before mounting.

A before-write advisory freezes the actual output. After considering it, repeat the original inputs with the returned references to request publication without rerunning the program. Exact per-invocation approval still applies; the reference itself grants nothing. Permission to execute native code does not waive checks on the as-yet unknown output. Revise a violating producer or use the existing exact file-candidate exception path; unknown/failed mandatory checks never become approval.

CUSTOM_TOOL_EXECUTION_FAILED preserves the program's failure data under tool_output and withholds its private changes. Inspect the failure and current evidence before another execution. Frozen outputs, partial formal commits and failed observation must not be blindly replayed.

Pure transforms and Host bridge interpreters have disposable private views; their data or call requests do not grant formal filesystem access. The bridge's real leaf calls still pass through the existing permission and file-service paths. Registration self-test effects are discarded; only the explicitly registered asset is saved. Names from installed built-in capability/runner/completion interfaces cannot be used as custom identities, including when mounting an old asset. A returned definition_digest or built-in-looking error field is program data, not a Host receipt or storage diagnosis.
