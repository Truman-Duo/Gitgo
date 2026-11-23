"""Host-selected Python library objects for the native read-only runner.

The interpreter is unchanged. Grant its executable/stdlib objects, not every
installed development dependency. Format adapters declare their import closure
here; missing optional packages still follow the adapter's normal error path.
These are runtime inputs, never a model-supplied module/path allowlist.
"""
from importlib.util import find_spec
from pathlib import Path
import stat
import sys


_DOCUMENT_MODULES = {
    '.pdf': ('pypdf', 'PIL', 'cryptography', 'cffi', '_cffi_backend', 'pycparser'),
    '.docx': ('docx', 'lxml', 'typing_extensions'),
    '.pptx': ('pptx', 'lxml', 'PIL', 'xlsxwriter', 'typing_extensions'),
    '.xlsx': ('openpyxl', 'et_xmlfile', 'defusedxml', 'lxml', 'PIL'),
    '.xlsm': ('openpyxl', 'et_xmlfile', 'defusedxml', 'lxml', 'PIL'),
    '.xls': ('xlrd',),
}
_PYTHON_FILE_TYPES = {'.exe', '.dll', '.pyd', '.zip', '._pth'}


def reader_python_roots(prefix, handler, target):
    prefix = Path(prefix).resolve(strict=True)
    selected, shallow = [], [prefix]
    def checked(path):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            from .sandbox import SandboxDenied
            raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Reader runtime input is a link or reparse point',
                                details={'runtime_path': str(path), 'phase': 'runtime_selection'})
        return path
    # Embeddable CPython places stdlib.zip and native extensions beside the
    # executable; regular installations also use Lib and DLLs. Do not recurse
    # into site-packages, Scripts, build tools or developer evidence.
    for path in prefix.iterdir():
        if path.is_file() and path.suffix.lower() in _PYTHON_FILE_TYPES:
            selected.append(checked(path))
    # A regular Windows venv keeps its launcher in Scripts. Preserve that
    # exact executable and interpreter configuration without exposing Scripts.
    executable = Path(sys.executable).absolute()
    if executable.is_relative_to(prefix) and executable not in selected:
        selected.append(checked(executable))
        shallow.append(executable.parent)
    configuration = prefix / 'pyvenv.cfg'
    if configuration.is_file():
        selected.append(checked(configuration))
    library = prefix / 'Lib'
    if library.is_dir():
        shallow.append(checked(library))
        selected.extend(checked(p) for p in library.iterdir() if p.name.casefold() != 'site-packages')
    extensions = prefix / 'DLLs'
    if extensions.is_dir():
        selected.append(checked(extensions))
    modules = _DOCUMENT_MODULES.get(Path(target).suffix.lower(), ()) if handler == 'document_open' else ()
    for name in modules:
        spec = find_spec(name)
        if spec is None:
            continue
        paths = tuple(spec.submodule_search_locations or ())
        if not paths and spec.origin and spec.origin not in {'built-in', 'frozen'}:
            paths = (spec.origin,)
        for raw in paths:
            path = checked(Path(raw)).absolute()
            selected.append(path)
            shallow.append(path.parent)
    if not selected:
        from .sandbox import SandboxDenied
        raise SandboxDenied('SANDBOX_POLICY_INVALID', 'Source Python reader runtime layout is unavailable')
    return selected, shallow
