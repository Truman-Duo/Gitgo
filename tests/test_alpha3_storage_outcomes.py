"""Alpha3 knowledge/scratch pointers preserve uncertain commit acknowledgement."""
from contextlib import closing
import sqlite3

import pytest
from backend.core.knowledge.models import Lesson
from backend.core.knowledge.review import content_version, proposal_version, reviewed_record
from backend.core.storage import StorageCommitUnknown
from tests.test_storage_transaction_outcomes import runtime, inject, ResponseLost


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.parametrize("operation", ["retrieval", "verification", "checker", "proposal", "revision"])
def test_alpha3_services_never_reuse_writer_after_commit_response_loss(runtime,mode,operation):
    lesson=Lesson(id="fault-lesson",project_name="project",trigger="sample.py",rule="Use the verified API.")
    record=reviewed_record(lesson.to_dict(),kind="author_supplied",reference="test-fixture")
    runtime.upsert_lesson_record(record,scope="instance",content_hash="fixture")
    if operation == "retrieval":
        call=lambda:runtime.record_lesson_retrieval(lesson.id,"2026-10-19T00:00:00Z")
    elif operation == "verification":
        call=lambda:runtime.record_lesson_verification(lesson.id,runtime.paths.project_id,"project",
            user_confirmation=True,expected_version=content_version(record))
    elif operation == "checker":
        change={"lesson_id":lesson.id,"project_id":runtime.paths.project_id,
            "expected_version":content_version(record),"action":"disable","check":None}
        call=lambda:runtime.configure_lesson_checker(change,grant_id="test-owned-approval")
    elif operation == "proposal":
        runtime.save_harvest_signal_records([{"signal_id":"hs_fault","state":"awaiting_confirmation",
            "project_name":"project","proposal_id":"proposal","proposal_lessons":[]}])
        version=proposal_version("project","proposal",["hs_fault"],[])
        call=lambda:runtime.resolve_harvest_proposal_records("project","proposal",accepted=False,
            candidates=None,expected_version=version)
    else:
        ref=runtime.put_blob(b"immutable scratch revision")
        call=lambda:runtime.commit_state_revision("workbench_sheet","head",ref,
            expected_ref=None,revision_key="revision-1")
    if operation == "revision":
        query="SELECT value_ref FROM storage_kv WHERE namespace='workbench_sheet' AND key='head'"
    elif operation == "proposal":
        query="SELECT payload_ref FROM governance_signals WHERE signal_id='hs_fault'"
    else:
        query="SELECT record_ref FROM lessons WHERE lesson_id='fault-lesson'"
    def read_ref():
        with closing(sqlite3.connect(runtime.paths.state_db.as_uri()+"?mode=ro",uri=True)) as connection:
            row=connection.execute(query).fetchone()
            return row[0] if row else None
    before=read_ref()
    with inject(runtime,mode) as fault:
        with pytest.raises(StorageCommitUnknown) as caught:
            call()
        assert fault.fired and caught.value.stage == "commit"
        assert isinstance(caught.value.__cause__,ResponseLost)
        assert runtime._state.quarantined
        with pytest.raises(StorageCommitUnknown):
            runtime.put_blob(b"must not publish a retry")
    assert (read_ref() != before) is (mode == "after")
