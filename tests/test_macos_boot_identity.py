"""Clock correction must neither revoke a live lease nor simulate a reboot."""
import json
from types import SimpleNamespace
import pytest
from backend.core import macos_identity as identity, macos_execution_service as service

BOOT = '1712b6c7-f85c-42cc-9780-851da992c246'
OTHER = '2712b6c7-f85c-42cc-9780-851da992c246'


def test_calendar_correction_does_not_change_execution_identity(monkeypatch, tmp_path_factory):
    calendar = iter([197143, 136146])
    def sysctl(argv, **kwargs):
        if argv[-1] == 'kern.boottime':
            return SimpleNamespace(stdout='{ sec = 1794196892, usec = %s }' % next(calendar))
        assert argv == ['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid']
        return SimpleNamespace(stdout=BOOT.upper() + '\n')
    monkeypatch.setattr(identity.subprocess, 'run', sysctl)
    root = tmp_path_factory / 'lease'; root.mkdir()
    state = dict(schema=1, uid=200970322, phase='prepared', generation=0,
                 launch_count=0, boot_id=identity.boot_id(), clock='CLOCK_MONOTONIC_RAW')
    (root / 'state.json').write_text(json.dumps(state))
    monkeypatch.setattr(identity, 'bounded_read', lambda p, *a: p.read_bytes())
    assert service.read_state(root) == state
    monkeypatch.setattr(identity, 'boot_id', lambda: OTHER)
    with pytest.raises(RuntimeError, match='boot session mismatch'):
        service.read_state(root)


@pytest.mark.parametrize('value', ['', 'unknown', '{ sec = 123, usec = 456 }',
    '00000000-0000-0000-0000-000000000000', BOOT + '\nextra', BOOT.replace('-', '')])
def test_missing_or_malformed_kernel_uuid_never_falls_back(monkeypatch, value):
    monkeypatch.setattr(identity.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=value))
    with pytest.raises(RuntimeError, match='identity unavailable'):
        identity.boot_id()


def test_kernel_read_failure_is_not_a_cached_boot(monkeypatch):
    def fail(*a, **k): raise OSError('sysctl unavailable')
    monkeypatch.setattr(identity.subprocess, 'run', fail)
    with pytest.raises(OSError): identity.boot_id()


@pytest.mark.parametrize('value', [[1794196892, 197143], None, True, OTHER])
def test_old_or_other_boot_lease_cannot_launch(monkeypatch, tmp_path_factory, value):
    root=tmp_path_factory / 'lease'; root.mkdir()
    state=dict(schema=1,uid=200970322,phase='prepared',generation=0,launch_count=0,
               boot_id=value,clock='CLOCK_MONOTONIC_RAW')
    (root/'state.json').write_text(json.dumps(state))
    monkeypatch.setattr(identity,'bounded_read',lambda p,*a:p.read_bytes())
    monkeypatch.setattr(identity,'boot_id',lambda:BOOT)
    with pytest.raises(RuntimeError,match='boot session mismatch'): service.read_state(root)
