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

## SQL input validation and bounded fact retirement

Checkpoint publication now freezes SQL parameters before creating any object
file. SQL-only fields participate in the existing estimated write budget.
Invalid integer ranges, non-finite SQL numbers and unencodable text fail before
materialization. The final transaction consumes the frozen statements after its
State dependency check. This does not qualify physical SQL growth, historical
replacement costs, input heap usage or a durable reservation.

Fact acknowledgment and maintenance use the same uncertain-commit handling as
other authoritative transactions. Maintenance rechecks retention eligibility
after acquiring the writer transaction and removes at most the requested limit
(capped at 500) of facts and, separately, reference rows per pass. References
remaining after a fact retires continue to protect CAS objects. Further passes,
including after restart, drain those rows without reconstructing retired facts.
Failed or uncertain commits do not report successful cleanup. These limits bound
changed row counts, not lookup cost, SQLite pages or a hard cleanup-space reserve.

Regressions cover invalid SQL-only inputs, immutable publication parameters,
commit/rollback uncertainty, partial retirement across restart, active roots and
consumer/fact changes before acquiring the writer transaction. The CAS lifetime
and activation requirements below remain unchanged.

## Bounded publication profiles

The checkpoint repository now exposes the same immutable publication to its
planner and legacy writer. Planning creates no new object files or State rows.
The final owning transaction revalidates its captured dependencies and applies
the fixed SQL. Only confirmed commit returns the new revision.

An internal, inactive profile contract can additionally bound snapshot depth,
node count, UTF-8 bytes, SQL parameters/statements, generated CAS objects and
old replacement/history/reference rows. Input copying rejects cycles, custom
adapters and non-finite or out-of-range scalars before encoding. Bounded history
counts and dependency reads share one transaction snapshot; publication rechecks
the counts and the projected growth from newly retained facts and roots. Oversized
history is rejected without deleting it. The complete profile, footprint and
publication workload participate in the publication digest.

The WAL capacity value sums all business, cleanup and housekeeping attempts
since a confirmed reset, including retained partial tails. Its conditional
frame bound includes FULL-sync sector padding and derives the shared-memory
index size from the aggregate frame count. It supplies no reservation, refund,
checkpoint-reset or execution API. The main/WAL/SHM classes are only part of a
complete storage envelope.

There are no enabled production profile defaults. The frame derivation requires
a qualified SQLite build, fixed page/sector limits, disabled dirty-page spilling
and flushing, a closed transaction schedule, and native enforcement. Bounded
input work is not a native heap limit. Runtime identity, every concurrent file
class, durable debt accounting, prepared generation ownership and migration/
recovery qualification remain activation requirements. Legacy storage limits
and the CAS v2 refusal gate remain unchanged.

## Complete root discovery before collection

CAS inventory reports known direct, explicit-owner and nested roots separately
from discovery completeness. Missing, unreadable, over-limit, hash-mismatched or
malformed legacy checkpoint metadata produces an incomplete result. Duplicate
JSON fields are rejected instead of discarding one set of potential references.
These reads are bounded and do not reconstruct missing files.

Collection requires a complete inventory before admitting writes or unlinking
any candidate. An incomplete scan leaves files, ownership rows and the previous
maintenance timestamp unchanged. Authoritative metadata discovery failures also
appear as blocked storage health. Existing facts/reference rows still protect
their objects; unknown discovery is never converted into an empty root set.

This closes the incomplete-scan deletion path. It does not fence concurrent
publication, add generation-aware deletion, or activate CAS v2. All participants
still need the shared preparation/ownership/collection protocol below.

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


## Retained fact evidence in the publication plan

The shared checkpoint plan contains the exact immutable outbox rows it will
publish, including stream position, payload, causation, producer and schema.
The same record supplies SQL parameters; construction rejects any disagreement
between planned evidence and the closed SQL writes. The internal publication
digest version is advanced; no database format or applied migration changes.

Recovery can compare retained rows in its owned committed snapshot with this
evidence, without reconstructing a missing fact or replaying an execution. This
check is not a lease, commit proof by itself, or a reason to accept missing
history: retirement, preparation membership and native object generations must
still be established by the complete lifecycle protocol before CAS v2 activation.


## Native storage lifetime and registry records

Windows storage scope, process and journal components have explicit native ABIs
and real native fault tests. They are construction dependencies for the guarded
storage path; the default legacy StorageRuntime does not load them or activate
CAS v2. They do not constitute a qualified storage backend or an installation
approval. A trusted runtime owner must supply already loaded native modules
and retain the verified installation and namespace pins for their lifetime.
No production loader consults the test-library environment variable.

Native records own handles before creation, duplication, locking or process
launch. Fixed pools retain their slots after an unknown close. A successful
retirement whose return was lost can be observed again without closing a
reused numeric handle or touching a newer owner. Python garbage collection
cannot discard these owners. Scope owners bind to their native process and
thread. Process owners create the root suspended and atomically assign its
Job; the admission coordinator must commit the exact identity before resume.
The worker receives a closed environment, one-process and 256 MiB Job limits,
a 30-second aggregate user CPU limit and a hard CPU rate cap. A busy or failed
retirement cannot confirm cleanup or refund capacity. These trusted storage
workers are separate from the untrusted tool execution backend.

Worker reservations and installed-runtime records share one two-cell framing
implementation. Each journal is exactly 8192 bytes; an update writes one
4096-byte cell after comparing the entire prior snapshot, then flushes and
checks the complete readback. Checksums detect corruption and do not grant
authority. Torn nonempty peers, unknown versions, broken sequence links and
forged parsed snapshots are refused. Worker intent always includes the exact
selected runtime generation, manifest and activation epoch. Slot 63 belongs
to the runtime lifecycle; it cannot be consumed as a project reservation.

Activation retains the old runtime generation until its worker has retired,
native absence has been established and the committed record permits slot
clearance. The codecs check record transitions; the owning coordinator must
provide the actual gate, identity, admission and cleanup evidence. In
particular, writing RETIRED or ABSENT is not itself such evidence.

Build-only validation uses `scripts/build_storage_native.py` with an explicit
Clang or Zig compiler, a fresh output directory and `--selftest`. Both Windows
native CI paths build the actual sources and run the scope, process, journal
and record tests. Native binaries and fault executables are build outputs and
are not committed. These checks do not qualify source/frozen product routing,
canonical Host installation, migration, Linux storage primitives or macOS
storage primitives. Those gates remain required before guarded activation.


## Native metadata and installed runtime trees

Pinning, fresh installation, metadata bootstrap, absence observation and
exact-member retirement now share one four-owner native pool. Purpose is
immutable at reservation; lifecycle phases cannot select another purpose's
effects. Native failures seal the owner. Unknown closes retain it, while a
confirmed old retirement cannot touch a reused owner generation.

Bootstrap creates a new protected Host/System/Administrators ACL atomically.
It checks the actual process account, exact parent identity and parent ACL
before creation. A parent granting untrusted mutation or DELETE_CHILD is
refused; harmless read/traverse rights do not become inherited child authority.
Existing protected parents can be attached, but added members are still fresh
CREATE_NEW operations. Every metadata file must be flushed and read back before
completion. Fresh trees are not automatically adopted after a failure.

Installation has a closed 256-node, 64 MiB rounded file-data limit; bootstrap
has 32 nodes and 256 KiB. These are fixed primitive ceilings, not a Host budget,
volume free-space guarantee or filesystem metadata quota. The coordinator must
reserve all simultaneous copies and other resources before effects. Runtime
pinning checks exact identities, file contents and protection, and retains
native ancestor handles. Retirement names every allowed member; it never
walks or deletes an unrecorded tree. An omitted member prevents successful
root removal. Actual absence and confirmed owner closure remain separate
requirements before durable capacity refund.

Both Windows CI paths build this source and run real effects and C fault
injection. These primitives remain inactive construction dependencies until
the trusted installation, runtime selection, aggregate accounting and every
StorageRuntime caller have completed the same qualification. Their tests do
not establish product activation or sudden-power-loss durability.


## Closed runtime manifests and input-copy accounting

The runtime descriptor fixes every relative file name, exact length and digest,
the Python ABI and the storage profile. It is canonical and bounded, including
directories and the manifest itself in the native node limit. Windows device
names, file/directory conflicts and case aliases are rejected on every platform
that evaluates the descriptor. The compatibility contract includes all content
but excludes installation location and native identity; the installation
manifest separately binds both. Neither digest supplies installation approval.

An explicitly approved manifest can be pinned only after native enumeration
through each already owned directory confirms the complete expected member set.
No Python path enumeration opens a second search handle. Every member is then
opened relative to its pinned parent and its bytes are verified. Extra, missing,
replaced or changed members prevent readiness. An unknown read invalidates the
ready state and keeps all pins until confirmed close.

One pinned source and two installed copies are charged in the same existing
StorageEnvelope, with each file independently rounded and a full 64 KiB
manifest allowance per copy. This does not increase the Host limit or consume
a reservation. An underfunded envelope is rejected. Source-package approval,
Host installation, active-runtime selection and process-lifetime joins are
still required before these interfaces can activate a guarded storage path.


## Canonical Host bootstrap and prepaid slots

The Windows Host construction dependency obtains its SID and profile from the
actual native token. Its public entry points locate only
`<native-profile>/.gitgo/storage-authority-v1`; `state_home`, environment and a
project path cannot select another Host budget. Before opening or creating this
authority, it checks the actual parent object for untrusted mutation rights,
including DELETE_CHILD, and qualifies the exact local NTFS object and 4096-byte
allocation unit. Existing parents receive the same checks as new installation.
No ACL repair or adoption of a partially initialized authority is performed.

Bootstrap holds a native gate, flushes the intent before other metadata, creates
the finite namespace, and writes the authority record last. The record binds
account, parent/root/member identities, content contract and the same envelope.
The one charged input copy is fixed by its canonical root, actual root identity,
complete manifest digest and content contract in both the intent and authority.
A second copy with identical bytes cannot reuse that input allowance. The input
and Host trees must be disjoint before bootstrap can create any directory.
Missing or unpinned input blocks installation; it does not refund its charge.
Reopening rechecks those identities, protected metadata, complete namespace and
both journal types while holding the gate. A content/profile mismatch requires
an explicit migration; it cannot create a different authority location.

Each project capacity ticket follows a confirmed native slot-journal update.
An unknown update issues no ticket and keeps the reservation charged. Reopening
does not replay an existing reservation or refund it. Parent owners register
each child object before its native constructor can have effects. Failed child
closure prevents release of the outer gate and namespace scope. Owner and
ticket use are bound to the creating process/thread.

The bootstrap primitive admits at most eleven project slots within its fixed
node/byte limits; this is not a qualified production capacity profile. Tests
with two small prepaid projects validate shared slot accounting, not the full
State/Obs/WAL budget. Registered project references, runtime activation and
project workers still need their actual binding/lifetime joins. Unqualified
reference files are refused. The new path remains inactive in StorageRuntime.
Native Host protocol tests use new owned temporary parents; public canonical
installation still requires disposable-account source/frozen acceptance.


Host input exclusion also compares actual native ancestor identities before
creating the canonical parent or authority. A path spelling or directory alias
cannot put that authority inside the charged input copy. Runtime pin wrappers
retain their native leaf before construction can have effects, preserving the
same outer-owner close order if the constructor never returns.

Gate handoff ends the old owner's admission before closing its exact lock
handle. Unknown handoff cannot resume admission or authorize launch. Other
namespace and code pins remain owned. A busy process retirement returns
incomplete and preserves all enclosing resources; only confirmed native
retirement allows their closure. Native current-process context observes the
real image, system directory and creation identity without another open handle
or environment-derived identity. This observation is not runtime approval.


## Owned native runtime installation

Runtime installation now consumes a closed plan bound to the canonical Host,
one of its two prepaid destination slots, a fresh generation, the one charged
input manifest and its complete content contract. Complete registered namespace
and absence checks precede the durable reservation. An unknown reservation
returns no live ticket; neither reopening nor owner closure refunds it.

The parent transfers the existing input pins into its owned lifetime without
creating a second input allowance. It records INTENT before native creation,
creates one suspended Job process, and persists its actual PID/creation identity
before gate handoff and resume. The worker accepts only its exact CREATED
record. Its executable, Python paths, loaded modules, environment and ABI must
match the approved input role. Installation writes only fresh bounded members,
flushes and verifies them, then records MATERIALIZED. Output and exit status
carry no authority. The fixed production entry accepts no Host-path override.

The parent first confirms native Job retirement and closes every original Host
resource. Only a newly attached canonical observer can independently pin and
verify all installed bytes and record READY. A failed or interrupted installer
remains RETIRING and charged. Lost terminal acknowledgement requires confirmed
flush and exact readback, or a new evidence commit after prior writers close;
the worker is never replayed. READY does not activate the installed runtime.

The explicit builder assembles CPython and declared application dependencies
into a closed input directory; it does not download, install, change permissions
or approve its own output. Windows 10+ supplies the OS UCRT, so downlevel local
UCRT copies are excluded rather than weakening module-role checks. Both native
Windows CI jobs run the public canonical-account entry on their disposable
runners. Local tests instead use fresh private account-directory fixtures;
only the test entry substitutes that internal attachment. Neither path qualifies
the full State/Obs profile, frozen Host bootstrap, native VFS, or the complete
StorageRuntime API. Those remaining joins still block guarded activation and
completion of the storage milestone.


## Runtime leases, activation, cleanup and crash recovery

The existing `runtime.lease` object now supplies shared selection leases and
exclusive lifecycle leases. Acquisition occurs under the canonical Host gate
and binds its recorded native file identity. A parent retains the lease through
gate handoff, the worker's actual native retirement and all enclosing resource
closures. The child consumes the exact persisted launch; it does not reacquire
the parent's exclusive lease. A fresh observer acquires its own exclusive lease
only after the original owner closes. Unknown acquisition or close preserves
debt and disables further admission.

Selection requires the active generation, full content pins and exact epoch;
it confirms the current runtime journal's flush and readback before returning a
live reference. It is not a storage-operation ticket. Its shared lease survives
gate handoff and blocks installation, activation and cleanup until owner close.
Activation independently verifies READY bytes, then atomically advances epoch,
switches ACTIVE and marks the old copy RETIRING in the same journal update.
The old copy remains charged. Different build/content profiles remain refused.

Cleanup uses the same persisted INTENT/CREATED/ACCEPTED native launch protocol,
executing from the one approved input. It names only declared members, verifies
partial file bytes against the input, and deletes children before their exact
root. Worker exit or diagnostic output cannot establish absence. After native
and original Host closure, the new observer verifies actual absence and closes
that observation before recording ABSENT. A separate operation rechecks absence
and commits slot removal before capacity becomes available. Interrupted cleanup
keeps the generation charged; a later separately admitted cleanup verifies the
remaining members. A present root whose creation identity was never recorded
is refused before cleanup INTENT, without adopting its identity or deleting it.

Crash recovery accepts only persisted exact parent and child process identities.
It independently proves both have ended, closes every observation with a known
result, and verifies either all materialized bytes or actual cleanup absence
before advancing the journal. A living parent, missing child identity, unknown
native query, or unknown observer close prevents retirement. The worker is never
replayed. A cancelled reservation with no launch still needs a proper cleanup
receipt before its slot can be released.

Journal capacity includes the remaining allowance of both slots. Ordinary
progress spends prepaid updates; the post-failure cleanup allowance remains
charged, and each new cleanup attempt must fund its additional updates before
INTENT. Neither a retry nor a new slot resets the monotonic sequence. This is
bounded journal accounting, separate from file, memory, process and volume
qualification. Actual multi-project State/Obs/WAL accounting remains required.

Local acceptance includes real installer/cleanup jobs, incomplete deletion,
atomic same-build upgrade, lease contention, acknowledgement loss and real
parent-process exit. Both Windows native CI paths additionally exercise public
canonical-account install, activate, upgrade, cleanup, refund and selection on
disposable runners. These construction tests do not enable the default storage
format or establish complete source/frozen product or power-loss acceptance.


## Native SQLite file ownership construction

The Windows VFS binds the actual imported SQLite module, not a library basename
or reported version. Bounded PE import inspection compares every CPython SQLite
import with the independently selected loaded module. The explicit `win32` VFS,
its callbacks and each returned file-method table must belong to that image.
The runtime version must match the explicitly supplied build header. CI obtains
that header from its already pinned official release archive. A byte-identical
separately loaded SQLite library is rejected. The default VFS is never replaced.

All file callbacks execute in C with independent fixed native storage. Each
worker lifetime permits eight VFS registrations and, per registration, sixteen
parent-file opens and sixteen connection acquisitions. Nominal SQLite close
does not refund these lifetime limits. The enclosing native worker is still
responsible for actual physical retirement, including hidden SQLite/OS state.
Process and thread identity constrain registration, callbacks and retirement.

Each registration confines SQLite to one main/WAL/rollback-journal family and
its bounded SHM extent. Byte ceilings precede writes, truncation and SHM mapping;
existing oversized family members refuse admission. Temporary disk files,
foreign attachments, extension loading, raw file controls and database mmap
are unavailable through this VFS. Missing extent evidence is distinct from a
measured empty file. This primitive is not a path-race or ACL defense: actual
namespace pins, file identities, exclusive admission, runtime selection and
durable budget intent remain mandatory enclosing contracts.

Python owns the registration before native creation and a connection slot before
lease acquisition or SQLite initialization. The actual connection object is
retained before initialization. Lost open, close or lease acknowledgements keep
the slot charged; they never trigger implicit replay, repeated native release
or garbage-collection refund. Native close/unmap uncertainty preserves the
independent file storage. I/O failures seal further work while known cleanup
remains possible. Documented short reads and bounded refusals retain their
normal SQLite meaning. A failed unregister is never retried by the Python
owner. Only the complete enclosing native-process retirement can settle debt
that the connection protocol cannot conclusively close.

Acceptance includes actual WAL writes and overflow rollback, pinned readers,
foreign-path refusal, distinct-module binding refusal, per-process quotas and
native/Python acknowledgement-loss faults. These tests qualify construction
boundaries only. The actual State/Obs profile, protected namespace integration,
SQL/CAS publication, recovery/collection and all StorageRuntime callers remain
necessary before guarded format activation or storage-milestone completion.


### Native CAS object ownership and repository quarantine

The Windows construction layer now owns finite CAS creation and generation
lifetimes in native storage. Eight creation slots and 32 read/retirement slots
are lifetime quotas in one worker; nominal handle closure does not replenish
them. Every open binds the retained native parent and recorded file identity.
Creation uses an exact digest/generation name, create-only disposition, a fixed
64 KiB content limit and 4 KiB writes. The coordinator must persist the actual
birth identity before writing bytes. A crash before that identity is committed
leaves unknown debt and never permits adopting the file by name.

Read and committed-retirement purposes require the exact full length and
digest. A separate immutable unpublished-retirement purpose accepts a shorter
file only against the recorded birth identity and verified prefix of the full
immutable input. The enclosing coordinator must prove that the batch did not
publish. Disposition is consumed before the OS effect. Unknown disposition or
close cannot be repeated; independent absence and authoritative retirement
remain prerequisites for refunding capacity.

The native SQLite owner uses the existing fixed repository connection class.
An unknown authoritative transaction quarantines that connection, its deferred
cursors and the enclosing VFS owner, preventing another connection from reusing
the uncertain writer. State and Observability migrations are exercised through
the actual native VFS. Their commits remain independent.

These are construction primitives and protocol tests. Protected project
registration, aggregate admission, durable publication and independent worker
recovery still have to join them before production CAS-v2 activation.


## Selected-runtime project initialization

The canonical Host's existing project slot now retains root and initial member
identities. No second project reference file supplies authority. Empty-project
records retain their previous encoding; an older decoder refuses an extended
record. Construction advances through BUILDING, STRUCTURED, MATERIALIZED,
VERIFIED and READY. The root identity precedes member creation. The complete
member identity map precedes every database effect. A present root without its
recorded creation identity remains unknown and cannot be adopted or recreated.

A separate immutable native DATA purpose pins metadata while allowing SQLite
writes. Its handles participate in Windows share checks and withhold deletion
sharing. Main files, gates and directories require exact recorded identities;
reparse points, aliases, extra links, permissive ACLs and oversized members are
refused. Only the six closed State/Observability sidecar names admit unbound
metadata inspection. Known sidecar probes close before SQLite owns their
removal, while parent pins and exclusive control/data/CAS leases remain held.
The fixed 32-node and 2 GiB logical inspection allowance is not an OS disk quota.
Missing names and closed probes still consume their lifetime allowance.

The pinned runtime may contain an explicit initialization-only storage profile.
It binds both complete database families, actual SQLite version, repository
migration checksums, pristine schema digests and closed operations. Its digest
must equal the selected runtime and Host reservation profile. The package
builder accepts this descriptor explicitly with --profile-file and verifies
its requested digest. This profile cannot approve checkpoint publication or
CAS cleanup, and the default StorageRuntime does not activate it.

The parent commits INTENT and the actual suspended child's PID/creation time
before resuming that child. Initialization executes the active installed
interpreter with its complete module pins and runtime lease. The child checks
its own native identity, exact persisted request and activation epoch before
ACCEPTED. It opens the real State and Observability schemas only beneath the
recorded project namespace and exclusive leases. Their SQL commits remain
independent. Each database retains its canonical Host/project/native identity
binding, and the legacy migration name becomes a guarded view. No compatibility
UDF bypasses that guard. Unknown commit or close seals further work.

MATERIALIZED alone never means READY. After the complete initializer and its
original Host resources have retired, a separately admitted selected-runtime
worker verifies committed schema, binding, physical layout and empty business
tables. It does not resume migrations. Only this worker can record VERIFIED.
Its native retirement and a fresh exact namespace observation precede READY.
Diagnostic output and exit codes cannot provide these facts. A lost terminal
journal acknowledgement requires exact state and durability confirmation;
execution is never replayed to obtain another response.

Crash recovery independently proves that both original native parent and child
have ended and that every observation closed. A live parent, missing child
identity or uncertain observation prevents retirement. Recovery preserves
partial construction and never adopts an unrecorded root. A complete committed
structure can undergo a later independent verification; an incomplete schema
cannot be completed by that reader.

Tests cover actual selected native jobs, each construction boundary, independent
verifier crashes, real parent exits, unknown native/SQL/journal responses,
identity and ACL refusal, and legacy writer refusal. Both Windows CI paths
include public canonical project initialization and verification in the same
disposable Host used for runtime lifecycle acceptance. The explicit serial
checkpoint profile below extends this foundation. Default legacy routing,
automatic migration, different-build upgrades and new platform backends are
separate work; they are not silently enabled by the checkpoint route.


## Complete immutable publication consistency

The checkpoint repository now validates the complete nested plan before it can
be used for admission. CAS members contain exact immutable bytes whose SHA-256
matches their descriptor; duplicate members and false aggregate sizes are
refused. State dependencies and historical counts use immutable typed values.
SQL statements retain adapter-free scalar tuples and exact parameter costs.
Publication reconstructs the SQL digest from those statements, captured State
dependencies, process identities and full object descriptors. Replacing a
nested value while retaining an earlier digest is rejected before file effects.

This consistency check does not authenticate caller-supplied SQL or grant a
file ticket. Only the existing repository builds executable statements.
Durable preparation still has to bind the complete publication digest to the
selected runtime, actual native generations and prepaid cleanup capacity.


## First logical project registration

Physical native initialization intentionally leaves business tables empty.
The checkpoint repository accepts an explicit immutable `ProjectRegistration`
only with a qualified input budget and matching logical project. Its bounded
workspace hint is descriptive metadata, never filesystem access authority.
Snapshot and registration metadata consume one shared input allowance.

Planning observes the optional existing project row in the same State snapshot
as checkpoint dependencies. SQL filters oversized or non-text retained fields
before returning them to Python; the admitted owner must still bound SQLite
heap, pages and query work. An absent row produces a closed INSERT before the
session writes; an existing row must match and is not silently rewritten.
Publication rechecks the complete captured row, then commits registration,
checkpoint and outbox facts together. Failed or unknown COMMIT follows the same
quarantine and independent committed-fact read as other authoritative writes;
it does not replay registration in another transaction.

The SQL digest includes this captured registration under a new binding version.
Plans without registration retain their existing digest bytes and call path.
This optional value does not activate native checkpoint admission or CAS v2:
durable preparation, aggregate capacity, selected-worker coordination and
generation retirement remain required before that capability is enabled.


## Explicit bounded protected checkpoint route

`StorageRuntime.create_protected_project(registry, processes,
workspace_hint=...)` creates a new project in an already installed canonical
Windows Host whose pinned runtime carries the operational profile. It runs
the actual initializer and a separate verifier. Retain the returned `project`
and `slot`; `StorageRuntime.open_protected_project(...)` reopens that exact
registration. These APIs accept trusted loaded Host dependencies, never a
workspace-selected DLL, alternate state home, or implicit installation grant.

The returned facade provides the existing `save_agent_checkpoint(snapshot)`
and `load_agent_session_state(process_id)` methods, plus `recover()` and
confirmed `close()`. Supply `expected_revision` explicitly (zero for a new
process). Preserve the original bounded workspace hint when reopening; it is
immutable registration metadata and does not authorize filesystem access.
Other existing storage operations, including generic maintenance, fail before
legacy I/O. Default `StorageRuntime(workspace)` behavior and migration
checksums remain unchanged. This route does not migrate existing projects or
enable all Agent, observability, Shell, Git, network, or sandbox capabilities.

The pinned version-2 profile admits one serial preparation, eight CAS objects
per checkpoint and 32 physical generations in the project, including pending
and superseded copies. Each object is at most 64 KiB. Encoded operation input
is at most 16 KiB and also shares a node/depth/byte budget with registration.
Historical facts and logical roots are retained; reaching a bound refuses the
operation rather than truncating history or substituting empty data. These
bounds support the explicit checkpoint interface, not arbitrary workloads.
Input, schema and operation limits are part of the descriptor digest.

The Host commits the complete cycle debit and native launch INTENT together
in its existing slot journal. The actual PID/creation time, request bytes,
selected runtime and activation epoch must match before the worker accepts.
The repository creates the immutable SQL/CAS plan; the worker commits complete
preparation before file creation and actual native birth identity before bytes.
State, outbox, logical project registration, current-digest mapping and physical
generation ownership join one transaction. A present name with no recorded
birth identity is never adopted or deleted during recovery.

Each cycle reserves 28 worst-case WAL transaction attempts, including normal
publication, cleanup, one bounded reconciliation for each cleanup category and
housekeeping. An attempt is consumed before setting the native write ceiling or
entering SQL. Unknown limit/COMMIT/extent outcomes seal the original owner and
retain the entire range. The closed cleanup inventory reserves ordinary B-tree
and overflow payload, transient DELETE balancing and fixed future metadata;
it is valid only for the admitted schema and statement family. The native VFS
still enforces main/WAL/journal/SHM ceilings and disables mmap/disk temporaries.
The inspected SQLite implementations are 3.53.2 and 3.53.4; other versions cannot
enable this profile without new qualification. The actual library bytes are
bound by the approved runtime manifest, not merely a version string.

The publishing worker's output is diagnostic. After its entire native lifetime
retires, another selected worker checks committed facts and exact CAS bytes.
Only that observation yields a checkpoint reference. Known publication and
cleanup are separate: `StorageCleanupIncomplete.committed_reference` reports a
verified checkpoint when cleanup still needs reconciliation. An unconfirmed
verification raises `StorageCommitUnknown`; no publication is replayed to
recover its response. Missing or uncertain output cannot become a later result.

Cleanup first persists its exact retirement intent. It removes unpublished
objects only against retained planned bytes and native identity; collection
removes only superseded physical copies while retaining the verified current
copy and every historical logical digest. Unknown absence remains charged.
A reset worker verifies the remaining ownership and zero WALs, closes its SQL
owners, then records VERIFIED. The parent records SETTLED only after complete
native retirement and another exact namespace/zero-WAL observation. Runtime
installation or activation changes are blocked while any cycle remains
unsettled, so cleanup cannot lose its original runtime identity between workers.

Source and frozen acceptance exercise real selected native jobs in owned
temporary-account fixtures. A separate disposable-CI test exercises the public
canonical locator and unmodified `-m` worker entry. Parent death, unknown child
or file birth, input substitution, missing output, publication/cleanup crashes,
revision/capacity refusal and source/frozen reopen are independent test cases.
These tests do not claim power-loss durability or new platform backend support.

The runtime builder includes the Conda `libbz2.dll` and CPython 3.14 `zstd.dll`
dependencies beside their pinned extensions. A frozen parent's DLL search
directory can otherwise affect child loading; native module origin checks stay
enabled. See the [PyInstaller external-program guidance](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application)
and [Windows DLL search order](https://learn.microsoft.com/en-us/windows/win32/dlls/dynamic-link-library-search-order).
