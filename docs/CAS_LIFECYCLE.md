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
is stable and remains on disk. Each attempt opens an independent handle and
acquires the OS lock without waiting; the native lock excludes both threads
and processes. Contention reports `STORAGE_CAS_LIFECYCLE_BUSY`; unexpected
lock I/O failures propagate. Handles are non-inheritable. The process-local
coordinator tracks current token identity, without holding a Python mutex
across the operation.

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

Each borrow has an owner-private identity registered under the lease mutex,
with exception cleanup established before registration. Exiting a failed
borrow removes only its own registration. A rejected shutdown cannot clear
another shutdown attempt; an interrupted admitted attempt stops borrowing but
can be retried without resetting internal state.

CAS registers its file handle before native lock acquisition. Even if the OS
locks successfully and the wrapper raises before returning its result, the
registered handle is closed. Only the current token can clear its coordinator
identity. Explicit unlock failure still attempts handle closure. If closure
does not confirm `closed`, the ended borrow remains as cleanup debt and stops
new borrowing. Shutdown retries only these ended operations' exact handles;
it never infers completion of a live borrower from PID, thread age or timeout.
Until cleanup succeeds, direct lease close cannot bypass the maintenance
barrier. These records are host-private bookkeeping, never caller-supplied
release credentials or proof of durable execution/budget cleanup.

The tested interruption model is one admission/body exception followed by
normal unwinding, plus observable close failures with explicit retries. It is
not a guarantee against arbitrary repeated asynchronous exceptions during
cleanup, process kill or power loss. Host cancellation must eventually use
controlled observation boundaries and protect cleanup from repeated cancel;
the storage library does not install global signal handlers.

Native tests must qualify independent-handle, thread and process exclusion on
Windows, Linux and macOS, including POSIX fork without unlocking the parent.
Local CI results do not qualify NFS, SMB or unknown mounts. Before writer/GC
integration, storage placement must have an explicit supported lock contract;
an unqualified environment must not enable CAS v2. This foundation does not
add filesystem qualification or enable the macOS sandbox backend.

The primitive currently has no writer/collector call sites. Future integration
must acquire it before connection and CAS locks, drain operations before Runtime
shutdown, keep callbacks outside critical sections, and remove CAS file writes
from inside checkpoint SQL transactions. It must also protect the interval
between looking up a reference and reading its file.

## Cached Runtime retirement

The process-owned fallback pool keeps a strong entry until Runtime.close()
returns successfully. Release, LRU eviction, replacement by an injected facade,
and close_owned_storage() use the same retirement path. A failed close seals
that entry against reuse and retains its capacity charge. Replacement and
eviction cannot create or publish another facade by dropping that debt.

Only one identified closer may act on an entry. The pool mutex protects state
transitions, not Runtime I/O or callbacks. Concurrent/reentrant attempts report
busy and cannot clear the original closer. Old retirement references cannot
remove or close a later entry. A successfully closed Runtime whose response was
lost remains retained until its normal idempotent close confirms completion.

Bulk close attempts every current entry after an ordinary failure and reports
the failure; cancellation still propagates. Interpreter exit reports incomplete
cleanup rather than erasing ownership. Explicit release or bulk close retries
retained entries. Injected facades remain caller-owned, and a retiring fallback
cannot escape cleanup by becoming injected. An inherited pool is refused before
using its parent's mutex or writers; start a fresh process for a new pool.

Capacity is reserved before construction, and a shutdown request while a
constructor runs prevents its result from becoming available. This does not
qualify Runtime's internal partial-construction cleanup, a constructor return
handoff interrupted before the pool receives it, or native VFS close outcomes.
Those still require the compound owner and qualified database boundary before
CAS v2 activation. The pool is not a Host-wide byte quota or a drain registry
for directly constructed/caller-owned Runtimes.

## Inactive storage envelope contract

`StorageEnvelope` is an immutable, bounded value describing all simultaneous
file copies for one store and for its Host registry. It grants no allocation,
file creation or execution rights. No Runtime, writer or collector consumes it
yet, and it supplies no default policy or new configuration authority.

For each file class, charge every retained copy at its individual ceiling
rounded up to the qualified allocation unit. Registry files use the same rule;
rounding an aggregate after adding small files would undercount their space.
The complete descriptor must satisfy:

```text
file_charge = ceil(max_bytes / allocation_unit) * allocation_unit * max_files
store_charge = sum(store file_charge)
registry_charge = sum(registry file_charge)
registry_charge + slots * store_charge <= host_max_bytes
```

The descriptor bounds positive integers, class counts and slot counts. Unknown
versions/fields, duplicate or unsorted class names, booleans as integers,
arithmetic overflow and underfunded profiles are rejected. Encoding is bounded,
canonical JSON with duplicate-key rejection. A digest identifies these exact
bytes; it is neither a reservation ticket nor proof of enforced ceilings.

Each backend must qualify the actual allocation unit, complete role set,
maximum retained copies and enforcement for its supported filesystem and
runtime. An arbitrary caller-provided class list cannot qualify a backend.
This accounting excludes filesystem metadata and is not an OS volume quota,
a free-space reservation, a hard-process disk limit or a power-loss guarantee.
WAL, shared memory, rollback journals, publication copies, temporary files and
control/cleanup records require explicit ceilings before activating a writer.

The future registry must bind that exact profile to a trusted native Host
identity and pinned storage domain. Choosing another state_home, changing
environment variables or copying metadata must not create another budget
authority. Reserve the entire store envelope before creating any store files.
Incomplete initialization, a lost response and unconfirmed native closure
remain charged. A persisted reservation alone must not issue a fresh
initialization ticket. Unknown/torn records block admission rather than being
trimmed, skipped or replaced. Refund requires a separate fenced retirement
protocol with confirmed settlement of every dependent debt, never a timeout.

Registry installation, authoritative reservations, backend enforcement,
compound construction/cleanup and recovery are activation requirements, not
implemented behavior of this value type. The existing state_home selector is
unchanged; this contract does not make legacy storage globally quota-bound.

## Explicit repository transaction outcomes

All 17 remaining explicit repository transactions use the existing
`authoritative_transaction` boundary under their owning connection lock.
This includes State publication/removal and optional Observability imports,
Trace records and retention. Sharing outcome handling does not make
Observability authoritative and does not couple two database commits.

A COMMIT response failure is unknown whether the database committed or the
subsequent rollback succeeded. The affected writer is quarantined, the original
exception remains the cause, and no success value is returned. A body failure
with confirmed rollback preserves the original exception and permits reuse;
failed rollback also quarantines the writer. Cancellation keeps its exception
type while retaining quarantine. No automatic retry is introduced.

The duplicate session-event return still exits through confirmed commit.
Lesson deduplication after a competing insert is allowed only after confirmed
rollback; an IntegrityError from COMMIT cannot masquerade as that duplicate.
Tests inject response gaps both before and after real SQLite commits through
every affected public repository API and inspect durable rows with a separate
read-only connection. They also cover rollback failure and cancellation.

State uncertainty remains blocked. Observability uncertainty reports degraded
health without querying the sealed writer or relabeling it as corruption.
Diagnostics omit unavailable PRAGMAs, and new optional writes reject before
spending capacity or creating a detail file when the writer is already sealed.
Unaffected State writes can continue; shared CAS collection refuses incomplete
root discovery. Shutdown skips maintenance and still closes both connections.

This increment handles explicit repository SQL outcomes, not every autocommit
statement or constructor/migration boundary. Quarantine is connection-local,
not a durable preparation receipt. It cannot protect CAS files published before
SQL, reverse a collector unlink, settle unknown native close, or fence a writer
that passed a preflight before another thread quarantined its connection.
Durable preparation, compound ownership, fenced recovery and generation-aware
collection remain required before CAS v2 or durable approval activation.

## Checkpoint planning before publication

The legacy checkpoint writer copies its input, captures its State dependencies
with one SELECT under the owning lock, then builds the entire immutable CAS
closure in memory. The capture distinguishes process absence from revision zero
and includes process identity/revision/status/runtime reference, the latest
session-event sequence and count, latest terminal-barrier identity and count, the deletion
tombstone and the parent task link. An explicit stale revision or invalid
lifecycle transition refuses before file creation.

Lifecycle validation reads prior bytes outside the publication transaction,
without canonical-file repair, and checks the per-object ceiling and digest.
Missing or corrupt bytes fail explicitly. This legacy read does not provide
native object generation pinning; that remains a CAS v2 activation requirement.

All metadata/provider fragments, runtime, messages, task contract/result,
receipts, the checkpoint fact and optional terminal barrier are encoded before
materialization. Deduplicated bytes and descriptors are immutable. Legacy
per-object and aggregate CAS ceilings and the existing estimated SQL write
budget are checked before creating files. These checks are not a durable batch
reservation, free-space guarantee or bound on input/heap/actual SQL growth.

The final State transaction rechecks the captured dependencies before writing
rows. A changed dependency returns RevisionConflict; a committed tombstone
refuses resurrection. Event count also detects explicit-sequence backfill
into a gap below the last sequence. No automatic refresh or retry is performed. Both the
current checkpoint and historical fact own the complete prepared closure,
including the optional terminal payload. File creation and prior-object reads
are outside this transaction; checkpoint/fact/outbox/barrier publication remains
atomic. Only confirmed COMMIT returns a revision; uncertain COMMIT keeps the
existing writer quarantine and never authorizes another effect.

Tests exercise public deletion/event interleavings, an independent SQLite
writer changing captured dependencies, caller mutation, invalid/over-limit
plans, partial file materialization, corrupt/missing lifecycle bytes, complete
ownership, final SQL failure and terminal-checkpoint COMMIT response gaps.
Legacy files left before a failed publication are still not durable prepared
debts. The collector race, shared object-lifetime ordering and production batch
handoff are unresolved; CAS v2 and new durable grants stay disabled.

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
