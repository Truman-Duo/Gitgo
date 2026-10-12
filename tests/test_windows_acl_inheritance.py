from __future__ import annotations

import base64
import os
import ctypes as C
from pathlib import Path
import re
import subprocess
import sys

import pytest

from backend.core.executable_identity import windows_system_executable
from backend.core.windows_acl import SecurityTree
from backend.core.windows_security_descriptor import SecurityDescriptor
from backend.core.windows_security_privileges import security_privileges

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native Windows ACL inheritance")

@pytest.fixture(autouse=True)
def operator_privileges():
    try:
        with security_privileges():
            pass
    except PermissionError as error:
        if os.environ.get('GITHUB_ACTIONS') == 'true':
            pytest.fail(str(error))
        pytest.skip(str(error))


# Well-known principals avoid icacls account-lookup failures for fabricated
# account SIDs. Anonymous denial does not deny the elevated test operator.
_ORIGINAL_SID = "S-1-5-7"  # ANONYMOUS LOGON
_PROVISION_SID = "S-1-1-0"  # Everyone, read-only temporary diagnostic grant
_FUTURE_SID = "S-1-5-32-546"  # Builtin Guests


def _edit_acl(path: Path, *arguments: str) -> None:
    subprocess.run(
        [str(windows_system_executable("System32/icacls.exe")), str(path), *arguments],
        check=True, capture_output=True,
    )


def _write_dacl(tree: SecurityTree, path: Path, text: str) -> None:
    descriptor = C.c_void_p()
    tree.check(tree.from_sddl(text, 1, C.byref(descriptor), None))
    try:
        status = tree.set_object_security(tree.handles[str(path)], 4, descriptor)
        if status:
            raise C.WinError(tree.status_to_error(status))
    finally:
        tree.free(descriptor)


def _strict_state(parent: Path):
    with SecurityTree([parent], writable=False) as tree:
        return {str(Path(record['path']).relative_to(parent)):
                SecurityDescriptor.from_bytes(base64.b64decode(record['descriptor'])).comparison_key()
                for record in tree.snapshot()}


def _assert_same_state(baseline: Path, candidate: Path, stage: str) -> None:
    before, after = _strict_state(baseline), _strict_state(candidate)
    assert before.keys() == after.keys(), f"inventory differs at {stage}"
    for relative in before:
        assert before[relative] == after[relative], (
            f"descriptor/control differs at {stage}, object={relative}: "
            f"baseline={before[relative]!r}; restored={after[relative]!r}"
        )


def _make_pair_member(parent: Path, layout: str, flags: str) -> Path:
    parent.mkdir()
    # Both trees start with the same protected parent permissions, independent
    # of local/hosted runner defaults. OW explicitly exercises OWNER_RIGHTS.
    initial = (
        f"D:P(D;{flags};GW;;;{_ORIGINAL_SID})"
        "(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
        f"(A;{flags};GR;;;{_ORIGINAL_SID})"
    )
    with SecurityTree([parent]) as tree:
        _write_dacl(tree, parent, initial)
    _edit_acl(parent, "/setintegritylevel", "(OI)(CI)M")
    root = parent / "root"
    (root / "nested" / "deep").mkdir(parents=True)
    (root / "data.txt").write_text("root file", encoding="utf-8")
    (root / "nested" / "deep" / "data.txt").write_text("nested file", encoding="utf-8")
    if layout == "legacy_explicit":
        with SecurityTree([root]) as tree:
            # Fixture construction deliberately converts inherited ACEs into
            # legacy explicit ACEs; comparison below never performs this.
            for record in tree.snapshot():
                text = record['sddl'].split('S:', 1)[0]
                aces = re.findall(r"\([^()]+\)", text)
                explicit = []
                for ace in aces:
                    fields = ace[1:-1].split(';')
                    fields[1] = fields[1].replace('ID', '')
                    explicit.append('(' + ';'.join(fields) + ')')
                _write_dacl(tree, Path(record['path']), 'D:' + ''.join(explicit))
            for record in tree.snapshot():
                descriptor = SecurityDescriptor.from_bytes(base64.b64decode(record['descriptor']))
                assert not descriptor.control & (0x400 | 0x1000)
                assert all('ID' not in ace[1:-1].split(';')[1]
                           for ace in re.findall(r'\([^()]+\)', record['sddl'].split('S:', 1)[0]))
    elif layout == 'protected_child':
        _edit_acl(root / 'nested', '/inheritance:d')
        with SecurityTree([root / 'nested'], writable=False) as tree:
            descriptor = SecurityDescriptor.from_bytes(base64.b64decode(tree.snapshot()[0]['descriptor']))
            assert descriptor.control & 0x1000
    else:
        assert layout == "inherited"
    return root


def _add_descendants(root: Path, stage: str) -> None:
    # Direct and deeper directory/file creation exercises OI versus CI and
    # inherit-only/no-propagate effects, including behind a protected child.
    for directory in (root, root / "nested", root / "nested" / "deep"):
        added = directory / ("new_" + stage)
        added.mkdir()
        (added / "file.txt").write_text(stage, encoding="utf-8")
        (directory / ("new_" + stage + ".txt")).write_text(stage, encoding="utf-8")


@pytest.mark.parametrize("layout", ["legacy_explicit", "inherited", "protected_child"])
@pytest.mark.parametrize("flags", ["OI", "CI", "OICI", "OICIIO", "OICINP", "OICIIONP"])
def test_restore_preserves_future_parent_inheritance_exactly(tmp_path_factory: Path, layout: str, flags: str):
    baseline_parent = tmp_path_factory / "baseline_parent"
    candidate_parent = tmp_path_factory / "candidate_parent"
    baseline = _make_pair_member(baseline_parent, layout, flags)
    candidate = _make_pair_member(candidate_parent, layout, flags)
    _assert_same_state(baseline_parent, candidate_parent, "initial fixture")

    with SecurityTree([candidate]) as tree:
        original = tree.snapshot()
        _edit_acl(candidate, "/grant", f"*{_PROVISION_SID}:(OI)(CI)R")
        _edit_acl(candidate, "/setintegritylevel", "(OI)(CI)L")
        # Prove the fixture actually required restoration.
        assert [record["sddl"] for record in original] != [
            record["sddl"] for record in tree.snapshot()
        ]
        tree.restore(original, expected=tree.snapshot())
    _assert_same_state(baseline_parent, candidate_parent, "immediate restore")

    for root in (baseline, candidate):
        _add_descendants(root, "restored")
    _assert_same_state(baseline_parent, candidate_parent, "create after restore")

    icacls_flags = "".join("(" + flags[index:index + 2] + ")" for index in range(0, len(flags), 2))
    for stage, arguments in (
        ("grant", ("/grant", f"*{_FUTURE_SID}:{icacls_flags}R")),
        ("remove", ("/remove:g", f"*{_FUTURE_SID}")),
        ("reset", ("/reset",)),
    ):
        for parent in (baseline_parent, candidate_parent):
            _edit_acl(parent, *arguments)
        _assert_same_state(baseline_parent, candidate_parent, "parent " + stage)
        for root in (baseline, candidate):
            _add_descendants(root, stage)
        _assert_same_state(baseline_parent, candidate_parent, "create after parent " + stage)
