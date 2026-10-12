"""Select one dependency identity across the real macOS universal2 layout."""
import pytest
from scripts.provision_ci_sqlite import macos_sqlite_dependency


def test_universal2_duplicate_dependency_has_one_identity():
    library = '/Library/Frameworks/Python.framework/Versions/3.14/lib/libsqlite3.0.dylib'
    listing = ''.join(f'/tmp/_sqlite3.so (architecture {arch}):\n'
                      f'\t{library} (compatibility version 9.0.0, current version 9.8.0)\n'
                      '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1351.0.0)\n'
                      for arch in ('x86_64', 'arm64'))
    assert macos_sqlite_dependency(listing) == library


def test_static_sqlite_layout_requires_module_rebuild():
    assert macos_sqlite_dependency('/tmp/_sqlite3.so:\n'
        '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n') is None


def test_ambiguous_dependency_remains_rejected():
    listing = '\t/a/libsqlite3.dylib (compatibility version 1.0.0)\n' + \
              '\t/b/libsqlite3.dylib (compatibility version 1.0.0)\n'
    with pytest.raises(RuntimeError, match='dependency layout'):
        macos_sqlite_dependency(listing)
