# CAS lifecycle foundations

This increment adds format refusal and an ordering primitive. It does not
activate CAS lifecycle v2, convert existing stores, or fix the current online
collector. The durable approval path must remain disabled until preparation,
reference publication, deletion recovery and their callers share the protocol.

## Format admission

Before writer PRAGMAs or migrations, StorageRuntime checks the migration
history without changing it. A guarded history view, reserved protocol metadata,
unknown migration version or unreadable history cannot be treated as a new
database. Existing checksums remain authoritative. This runtime intentionally
does not register the future protocol SQL function or expose an upgrade API.

Reserved names are recognized regardless of ASCII letter case. Only an empty
application schema, or the empty history table left before the first migration
commits, can initialize without history. Existing application objects with
missing/empty history are refused. Accepted history is a checksum-verified
prefix of the known migrations; gaps, duplicates and non-integer versions do
not authorize repair or re-creation of history.

Offline salvage checks its isolated source before creating a destination.
Disposable reset and both relocation paths check the source under exclusive
maintenance before copying or replacing database families. These tools refuse
unknown formats rather than omit unfamiliar preparation or cleanup debt.

Recognizing future metadata is not an upgrade implementation. Activation still
requires an exclusive maintenance transition, a compatible recovery path,
supported-version/frozen checks and the complete writer/collector protocol.

## Ordering primitive

`CasLifecycleLock` accepts an active StorageLease. Its project-local lock file
is stable and remains on disk. Both the process-local coordinator and OS lock
are acquired without waiting; contention reports `STORAGE_CAS_LIFECYCLE_BUSY`.
Unexpected lock I/O failures propagate. Handles are non-inheritable.

The maintenance lease stays active for the entire operation and cannot be
closed during it. Explicit nesting requires a currently issued parent token
from the same lease and thread. Another Runtime, thread, expired token or
inherited process cannot borrow that operation. Child cleanup after POSIX fork
must not explicitly unlock the parent's open-file-description lock.

Runtime shutdown atomically stops new borrowing. Active operations cause a
retryable refusal before shutdown maintenance or connection closure. A failed
connection close keeps the maintenance barrier, blocks new operations, and can
be retried without re-closing confirmed closed connections. Direct lease close
cannot release this barrier after failed resource cleanup. Diagnostic failure
after confirmed connection closure still releases the lease. Concurrent or
callback-reentrant shutdown is refused; shutdown never waits for its own
operation. This is an idle-only close contract, not Host bounded drain.

The primitive currently has no writer/collector call sites. Future integration
must acquire it before connection and CAS locks, drain operations before Runtime
shutdown, keep callbacks outside critical sections, and remove CAS file writes
from inside checkpoint SQL transactions. It must also protect the interval
between looking up a reference and reading its file.

## Remaining protocol work

Writers need durable, budgeted preparation before creating or reusing files.
State references and preparation handoff belong in one transaction. Observability
publication needs a conservative handoff across its separate database. Unknown
commits retain protection and quarantine the affected writer.

The collector needs fresh roots under the same ordering domain, recoverable
deletion intents and exact object generations. Incomplete root discovery must
prevent deletion. It must never delete newly committed ownership by digest.
Process-exit tests do not establish power-loss durability of external CAS files.

No CAS lock is a substitute for keeping sandboxed processes out of trusted
storage. Native execution identity, quotas, launch fencing and confirmed process
tree cleanup remain separate requirements.
