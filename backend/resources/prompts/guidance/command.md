# Command

## Captured result and effects

Inspect captured exit code, stdout and stderr. A command may have changed files before returning nonzero; a timeout or failure is not proof of no effect. Check current state before rerunning an effectful script.

## Correct the command or executor

Correct the implementation, command arguments or shell dialect according to actual diagnostics. Use the tool's declared executor; Windows auto shell and explicit Bash have different syntax. If Bash identity verification fails, do not claim the script ran; repair/select a verified installation through the existing configuration path. Terminal fallback notices do not establish command failure.

## Bound the recovery

Keep testing bounded and do not repeat a command solely to recover already captured output.
