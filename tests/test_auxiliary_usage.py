"""Auxiliary cost observations include failed output and never invent zeros."""
import pytest
from backend.core.loop.provider_protocol import ProviderResponseIncomplete
from backend.core.loop.context_window import ContextWindow
from tests.test_manual_compaction import session_with_history

USAGE = {'input_tokens': 400, 'cache_read_tokens': 256, 'output_tokens': 50}


@pytest.mark.parametrize('incomplete', [False, True])
def test_compaction_emits_actual_usage_with_attempt_identity(incomplete):
    observations = []
    class Provider:
        max_output_tokens = 8192
        def chat(self, *args, **kwargs):
            if incomplete:
                raise ProviderResponseIncomplete('max_output_tokens', dict(USAGE))
            return {'content': 'Retain original obligations.', 'usage': dict(USAGE), 'response_id': 'observed'}
    session = session_with_history()
    result = ContextWindow().compact(session, Provider(), on_provider_notice=observations.append)
    assert result is not incomplete
    usage = [o for o in observations if o['event'] == 'auxiliary_provider_usage']
    assert len(usage) == 1 and usage[0]['usage'] == USAGE and usage[0]['usage_reported']
    assert usage[0]['usage_call_id'] and usage[0]['purpose'] == 'context_compaction'
    assert len(session.provider_usage) == 1


def test_missing_usage_is_unknown_not_zero():
    class Provider:
        def chat(self, *args, **kwargs): return {'content': 'Retain original obligations.'}
    observations = []
    assert ContextWindow().compact(session_with_history(), Provider(), on_provider_notice=observations.append)
    event = next(o for o in observations if o['event'] == 'auxiliary_provider_usage')
    assert event['usage_reported'] is False and 'usage' not in event


def test_harvest_reports_incomplete_and_completed_attempts_separately(tmp_path_factory):
    from backend.core.knowledge.harvest import harvest_llm_summary
    (tmp_path_factory / 'app.py').write_text('fixture', encoding='utf-8')
    observations, report = [], {}
    class Provider:
        max_output_tokens = 8192
        count = 0
        def chat(self, *args, **kwargs):
            self.count += 1
            if self.count == 1:
                raise ProviderResponseIncomplete('max_output_tokens', dict(USAGE))
            return {'content': '[]', 'usage': {**USAGE, 'output_tokens': 10}}
    harvest_llm_summary([{'signal_id': 'one', 'target_files': ['app.py']}], Provider(),
        str(tmp_path_factory), 'fixture', raise_on_error=True, on_provider_notice=observations.append, summary_report=report)
    events = [o for o in observations if o['event'] == 'auxiliary_provider_usage']
    assert len(events) == 2 and events[0]['incomplete']
    assert {o['usage_call_id'] for o in events} == {a['usage_call_id'] for a in report['attempts']}
    assert len({o['usage_call_id'] for o in events}) == 2
    assert sum(o['usage']['output_tokens'] for o in events) == 60
