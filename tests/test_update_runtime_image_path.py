"""Physical installation discovery must retain kernel identity in AppContainer."""
import os
from pathlib import Path
import sys

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows NT image namespace')
def test_nt_physical_image_identifies_the_same_executable():
    from backend.core.update.windows_storage import physical_executable
    image = Path(sys.executable)
    physical = physical_executable(image)
    assert str(physical).startswith('\\\\?\\GLOBALROOT\\Device\\')
    assert physical.name == image.name
    assert os.path.samefile(image, physical)


@pytest.mark.skipif(os.name != 'nt', reason='Windows AppContainer DOS mapping refusal')
def test_installation_discovery_uses_physical_image_on_mapping_refusal(monkeypatch):
    from backend.core.update import installation, windows_storage
    image = Path('C:/private/alias/gitgo-host.exe')
    physical = Path('C:/owned/active/internal/gitgo-host/gitgo-host.exe')
    original = Path.resolve
    def denied(path, *args, **kwargs):
        if path == image:
            error = PermissionError('DOS volume mapping unavailable')
            error.winerror = 5
            raise error
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', denied)
    calls = []
    monkeypatch.setattr(windows_storage, 'physical_executable', lambda path: calls.append(path) or physical)
    assert installation.physical_installation(image) == (Path('C:/owned'), 'active')
    assert calls == [image]
