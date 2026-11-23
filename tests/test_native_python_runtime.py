from pathlib import Path
from types import SimpleNamespace

from backend.core import native_python_runtime as runtime


def test_embedded_reader_excludes_unrelated_dependencies_and_developer_files(tmp_path_factory):
    tmp_path = tmp_path_factory
    for name in ('python.exe', 'python312.dll', 'python312.zip', 'python312._pth', '_ctypes.pyd'):
        (tmp_path / name).write_bytes(b'runtime')
    (tmp_path / 'notes.txt').write_text('private developer evidence')
    package = tmp_path / 'full-packages' / 'unrelated'
    package.mkdir(parents=True)
    (package / 'secret.txt').write_text('not an input to a reader')
    selected, parents = runtime.reader_python_roots(tmp_path, 'read_file', 'note.txt')
    assert {p.name for p in selected} == {'python.exe', 'python312.dll', 'python312.zip', 'python312._pth', '_ctypes.pyd'}
    assert parents == [tmp_path]
    assert not any(package.is_relative_to(p) for p in selected)


def test_regular_python_keeps_stdlib_but_not_site_packages(tmp_path_factory):
    tmp_path = tmp_path_factory
    (tmp_path / 'python.exe').touch()
    library = tmp_path / 'Lib'
    library.mkdir()
    (library / 'json').mkdir()
    (library / 'os.py').touch()
    (library / 'site-packages').mkdir()
    (tmp_path / 'DLLs').mkdir()
    selected, parents = runtime.reader_python_roots(tmp_path, 'list_files', '.')
    assert library in parents
    assert library / 'json' in selected and library / 'os.py' in selected
    assert tmp_path / 'DLLs' in selected
    assert library not in selected and library / 'site-packages' not in selected


def test_venv_launcher_is_available_without_granting_scripts(tmp_path_factory, monkeypatch):
    import sys
    scripts = tmp_path_factory / 'Scripts'
    scripts.mkdir()
    executable = scripts / 'python.exe'
    executable.touch()
    config = tmp_path_factory / 'pyvenv.cfg'
    config.write_text('home = base-python')
    (scripts / 'unrelated.exe').touch()
    monkeypatch.setattr(sys, 'executable', str(executable))
    selected, parents = runtime.reader_python_roots(tmp_path_factory, 'read_file', 'note.txt')
    assert executable in selected and config in selected
    assert scripts in parents and scripts not in selected
    assert scripts / 'unrelated.exe' not in selected


def test_format_imports_are_selected_only_for_document_adapter(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory
    (tmp_path / 'python.exe').touch()
    packages = tmp_path / 'packages'
    packages.mkdir()
    docx = packages / 'docx'
    docx.mkdir()
    extension = packages / 'typing_extensions.py'
    extension.touch()
    requested = []
    def spec(name):
        requested.append(name)
        if name == 'docx':
            return SimpleNamespace(submodule_search_locations=[str(docx)], origin=str(docx / '__init__.py'))
        if name == 'typing_extensions':
            return SimpleNamespace(submodule_search_locations=None, origin=str(extension))
    monkeypatch.setattr(runtime, 'find_spec', spec)
    text, _ = runtime.reader_python_roots(tmp_path, 'read_file', 'sample.docx')
    assert requested == [] and docx not in text
    document, parents = runtime.reader_python_roots(tmp_path, 'document_open', 'sample.DOCX')
    assert docx in document and extension in document and packages in parents
    assert set(requested) == {'docx', 'lxml', 'typing_extensions'}


def test_source_reader_layout_does_not_change_producer_or_packaged_roots(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory
    import sys
    from backend.core import sandbox_access as access
    backend = tmp_path / 'code' / 'backend'
    backend.mkdir(parents=True)
    prefix = tmp_path / 'python'
    prefix.mkdir()
    (prefix / 'python.exe').touch()
    (prefix / 'full-packages').mkdir()
    old = ([backend, prefix], [backend.parent])
    monkeypatch.setattr(access, 'native_runtime_access', lambda: old)
    monkeypatch.setattr(sys, 'prefix', str(prefix))
    monkeypatch.setattr(sys, 'base_prefix', str(prefix))
    roots, parents = access.native_read_runtime_access('read_file', 'note.txt')
    assert backend in roots and prefix / 'python.exe' in roots
    assert prefix not in roots and prefix in parents
    assert access.native_runtime_access() == old
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert access.native_read_runtime_access('read_file', 'note.txt') == old
