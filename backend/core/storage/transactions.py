"""Authoritative SQL transactions; uncertainty never grants another attempt."""
from contextlib import contextmanager

from .models import StorageCommitUnknown


@contextmanager
def authoritative_transaction(connection):
    """Use under the runtime State lock; never perform an external effect here.

    A failed COMMIT response is uncertain even if a later ROLLBACK succeeds.
    Losing the original exception or reusing the writer would erase that fact.
    """
    commit_attempted = False
    connection.require_available()
    if connection.in_transaction:
        raise RuntimeError("STATE_TRANSACTION_ALREADY_ACTIVE")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        commit_attempted = True
        connection.execute("COMMIT")
    except BaseException as error:
        rollback_failed = False
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except BaseException:
            rollback_failed = True
        if commit_attempted or rollback_failed:
            connection.quarantine()
            # Preserve shutdown/cancellation semantics while blocking this writer.
            if not isinstance(error, Exception):
                raise
            raise StorageCommitUnknown(
                "commit" if commit_attempted else "rollback",
                rollback_failed=rollback_failed,
            ) from error
        raise
