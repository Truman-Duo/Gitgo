# File access

## Choose the relevant evidence path

Use this guidance for a requested text-file read that returns FILE_NOT_FOUND, IS_DIRECTORY, BINARY_FILE or READ_ERROR. These are concrete target results; they do not establish that the reader, workspace or whole task is unavailable.

## Correct the target or reader

- FILE_NOT_FOUND: use current file discovery within the approved scope, or correct a path already established by evidence. A missing file is not permission or a request to create it.
- IS_DIRECTORY: list that directory or choose a specific file. Repeating the text read on the same directory does not add evidence.
- BINARY_FILE: use a mounted reader appropriate to the actual format. Do not claim text decoding succeeded or assume an adapter is installed.
- READ_ERROR: inspect error_info.details for the concrete read failure. Respect resource confinement and OS permissions; do not switch executors to evade them.

## Continue the original task

Read the corrected target if it is needed. If relevant evidence remains unavailable, state the material limit. A failed read does not require replaying a previously successful mutation or restarting the task.
