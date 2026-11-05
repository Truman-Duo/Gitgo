import json
import pytest
from backend.core.application import ApplicationServices,OperationError
from backend.core.llm_config import LLMConfigManager
from backend.core.loop.provider_router import RoutedProvider,BreakerRegistry


def create(service,name):
    return service.provider_save(name=name,model_id=name,base_url='http://127.0.0.1:11434/v1',kind='local',auth_mode='none')['provider']['id']


def test_each_api_owns_its_two_backups_across_switch_reopen_toggle_and_deletion():
    service=ApplicationServices();a,b,c,d=[create(service,n) for n in ('a','b','c','d')]
    current=service.provider_status()
    service.provider_failover_configure(True,[b,c],provider_id=a,expected_revision=current['failover_revision'])
    current=service.provider_status()
    service.provider_failover_configure(True,[c,d],provider_id=b,expected_revision=current['failover_revision'])
    service.provider_switch(b)
    assert service.provider_status()['failover_order']==[c,d]
    raw=LLMConfigManager.load()
    assert RoutedProvider(raw,registry=BreakerRegistry()).order==[b,c,d]
    assert raw['failover_routes'][a]==[b,c]
    service.provider_failover_configure(False)
    assert ApplicationServices().provider_status()['failover_routes']=={a:[b,c],b:[c,d]}
    service.provider_failover_configure(True)
    service.provider_delete(c)
    assert service.provider_status()['failover_routes']=={a:[b],b:[d]}
    service.provider_switch(a)
    assert RoutedProvider(LLMConfigManager.load(),registry=BreakerRegistry()).order==[a,b]
    # It never recursively follows b -> d while using a -> b.
    service.provider_delete(a)
    assert a not in service.provider_status()['failover_routes']


def test_invalid_or_stale_route_writes_are_atomic_and_do_not_change_other_owners():
    service=ApplicationServices();a,b,c,d=[create(service,n) for n in ('a','b','c','d')]
    current=service.provider_status()
    for order in ([b,c,d],[b,b],[a],['missing']):
        before=LLMConfigManager._config_path().read_bytes()
        with pytest.raises(OperationError):service.provider_failover_configure(True,order,provider_id=a)
        assert LLMConfigManager._config_path().read_bytes()==before
    service.provider_failover_configure(True,[b],provider_id=a,expected_revision=current['failover_revision'])
    before=LLMConfigManager._config_path().read_bytes()
    with pytest.raises(OperationError,match='FAILOVER_CONFIG_CHANGED'):
        service.provider_failover_configure(True,[d],provider_id=c,expected_revision=current['failover_revision'])
    assert LLMConfigManager._config_path().read_bytes()==before


def test_legacy_queue_belongs_only_to_the_original_primary_and_retains_its_full_migration_record():
    service=ApplicationServices();a,b,c,d=[create(service,n) for n in ('a','b','c','d')]
    path=LLMConfigManager._config_path();legacy=json.loads(path.read_text(encoding='utf-8'))
    legacy.pop('failover_routes');legacy['failover_order']=[b,c,d];legacy['failover_enabled']=True
    path.write_text(json.dumps(legacy),encoding='utf-8');before=path.read_bytes()
    current=service.provider_status()
    assert current['failover_routes']=={a:[b,c]}
    assert path.read_bytes()==before  # reading does not rewrite the fixture
    service.provider_switch(b)
    reopened=ApplicationServices().provider_status()
    assert reopened['failover_routes']=={a:[b,c]} and reopened['failover_order']==[]
    assert LLMConfigManager.load()['legacy_failover_order']==[b,c,d]
