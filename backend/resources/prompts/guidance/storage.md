# Storage

## Preserve retained evidence

Preserve retained diagnostics, immutable objects and completed effects. Do not edit databases, receipts, leases or checksums to make recovery appear successful. A stale or damaged checkpoint is not evidence that earlier writes never happened.

## Use the existing recovery interfaces

Use the existing storage health/maintenance and session recovery interfaces; backup and verified recovery may require user or operator action. A busy maintenance lease requires releasing the owning runtimes through their normal lifecycle.

## Stop unsafe writes

If authoritative objects are missing or storage safety is blocked, stop affected writes and explain the concrete limitation. Help lookup cannot repair state or authorize destructive maintenance.
