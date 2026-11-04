# Mechanism extension regression fixtures

These synthetic developer-authored and AI-assisted SDK fixtures were used during
implementation and debugging. They are development/regression material, not an
independent human-labelled holdout or evidence of real API acceptance.

The fixtures originally lived in ignored developer artifacts. The exact Python
sources required by the tracked regression tests now live here so a clean clone
can collect and execute those tests without private/local files. Original local
experiment copies and reports are retained separately.

These modules are loaded explicitly by tests; they are not installed as product
components and do not grant authority, initiate model requests or change policy.
