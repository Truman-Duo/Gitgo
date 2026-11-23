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
_PYTHON_FILE_TYPES = {'.dll', '.pyd', '.zip', '._pth'}


def reader_python_roots(prefix, handler, target, *, executable=None):
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
    # Select the launched interpreter and CPython's base interpreter for venvs.
    # Other executables in an installed prefix can be aliases (e.g. python3.exe).
    # Required images still pass the same pinned-path checks.
    images = (executable,) if executable is not None else (sys.executable, sys._base_executable)
    if handler == 'search_text':
        from .tools.workspace_search import resolve_ripgrep
        engine = resolve_ripgrep()
        if engine:
            images += (engine,)
    for image in dict.fromkeys(images):
        path = Path(image).absolute()
        if path.is_relative_to(prefix):
            selected.append(checked(path))
            shallow.append(path.parent)
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
