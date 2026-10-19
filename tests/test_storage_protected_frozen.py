"""Frozen Host facade and actual selected child, with owned account fixture."""
from pathlib import Path
import json,os,subprocess
import pytest
from tests.test_storage_operational_profile import operational_input
from tests.test_storage_protected_entry import fixture_route
from tests.test_storage_project_workers import initialized_runtime,PROJECT_FIXTURE
from tests.test_storage_windows_tree import native
from tests.test_storage_runtime_control import reopen
from backend.core.storage import StorageRuntime
from backend.core.storage.runtime_control import RuntimeControl
from backend.core.storage.runtime_lifecycle import select

@pytest.fixture
def frozen_host():
    value=os.environ.get('GITGO_SANDBOX_TEST_HOST')
    if not value:pytest.skip('Explicit built frozen Host required')
    path=Path(value).resolve(strict=True)
    assert path.is_file()
    return path

def test_frozen_public_entry_reopens_source_checkpoint_and_commits_next_revision(frozen_host,native,tmp_path_factory,operational_input,monkeypatch):
    build={'artifact':str(frozen_host)}
    with initialized_runtime(native,tmp_path_factory,operational_input,monkeypatch) as (registry,account,processes):
        with monkeypatch.context() as patch,fixture_route(patch,account,processes):
            with StorageRuntime.create_protected_project(registry,processes,workspace_hint='workspace metadata') as store:
                reference=store.save_agent_checkpoint(dict(process_id='p',session_id='s',messages=[],expected_revision=0,session_metadata={'marker':'source'}))
                assert reference.revision==1
                project,slot=store.project,store.slot
            host=reopen(registry,account);selected=select(RuntimeControl(host));manifest=selected.require_live();host.close()
            data=dict(manifest=manifest.to_bytes().decode('ascii'),approved=manifest.sha256,profile=account.profile,project=project,slot=slot,worker=PROJECT_FIXTURE)
            receipt=tmp_path_factory/'frozen-fixture.json';receipt.write_text(json.dumps(data),encoding='utf8')
            done=subprocess.run([build['artifact'],'--gitgo-internal-role','python','-B',str(Path(__file__).with_name('fixtures')/'storage_operational_frozen_probe.py'),str(receipt)],
                                cwd=Path(build['artifact']).parent,capture_output=True,text=True,timeout=90)
            assert done.returncode==0,done.stderr
            result=json.loads(done.stdout)
            assert result==dict(frozen=True,revision=2,launches=['read_checkpoint','publish_checkpoint','verify_checkpoint','retire_checkpoint','collect_obsolete','reset_wal','read_checkpoint'])
            with StorageRuntime.open_protected_project(registry,processes,project=project,slot=slot,workspace_hint='workspace metadata') as store:
                assert store.load_agent_session_state('p')['marker']=='frozen'
