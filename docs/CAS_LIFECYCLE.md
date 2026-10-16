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
