from dataclasses import replace
import hashlib
from pathlib import PurePosixPath
import uuid

import pytest

from backend.core.storage.native_windows_tree import StorageTreeError, TreePurpose
from backend.core.storage.runtime_manifest import RuntimeContent, RuntimeManifest, RuntimeManifestInvalid, RuntimeMember
from backend.core.storage.runtime_installation import PinnedRuntime
from tests.test_storage_windows_tree import native, fixture


def installation(trees, sid, parent, identity, root):
    files = {"app.zip": b"app", "lib/module.py": b"module", "python.exe": b"entry"}
    content = RuntimeContent("cpython-314-win_amd64", "a"*64,
        tuple(RuntimeMember(name, len(data), hashlib.sha256(data).hexdigest()) for name, data in sorted(files.items())))
    generation = str(uuid.uuid4())
    owner = trees.reserve(TreePurpose.INSTALL, sid)
    owner.begin(parent, identity, generation)
    directories = {"":0}
    for name in content.directories:
        directories[name] = owner.add(0, name, directory=True)
    for name, data in files.items():
        path = PurePosixPath(name)
        index = owner.add(directories.get(str(path.parent), 0), path.name, length=len(data))
        owner.write(index, data)
        owner.seal(index)
    manifest = RuntimeManifest(str(root/generation), owner.identity(0), content)
    raw = manifest.to_bytes()
    index = owner.add(0, "runtime-manifest.json", length=len(raw))
    owner.write(index, raw)
    owner.seal(index)
    owner.finish()
    owner.close()
    return manifest


def test_complete_inventory_and_pins_are_held_until_confirmed_close(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (_, trees, sid, root, parent, identity):
        manifest = installation(trees, sid, parent, identity, root)
        pinned = PinnedRuntime(trees, manifest.to_bytes(), manifest.sha256, sid)
        try:
            assert pinned.pin() is pinned and pinned.ready
            assert pinned.read("lib/module.py", 0, 6) == b"module"
            from pathlib import Path
            with pytest.raises(OSError):
                (Path(manifest.root)/"lib"/"module.py").write_bytes(b"change")
            with pytest.raises(RuntimeManifestInvalid):
                pinned.pin()
        finally:
            pinned.close()
        assert not pinned.ready
        with pytest.raises(RuntimeManifestInvalid):
            pinned.read("python.exe", 0, 1)


@pytest.mark.parametrize("change", ["extra", "missing", "changed", "root", "manifest"])
def test_partial_or_changed_installation_never_becomes_ready(native, tmp_path_factory, change):
    from pathlib import Path
    with fixture(native, tmp_path_factory) as (_, trees, sid, root, parent, identity):
        manifest = installation(trees, sid, parent, identity, root)
        installed = Path(manifest.root)
        if change == "extra": (installed/"extra.py").write_bytes(b"extra")
        elif change == "missing": (installed/"lib"/"module.py").unlink()
        elif change == "changed": (installed/"lib"/"module.py").write_bytes(b"mutate")
        elif change == "manifest": (installed/"runtime-manifest.json").write_bytes(b"{}")
        elif change == "root": manifest = replace(manifest, root_identity=replace(manifest.root_identity,file_id=manifest.root_identity.file_id+1))
        pinned = PinnedRuntime(trees, manifest.to_bytes(), manifest.sha256, sid)
        try:
            with pytest.raises((RuntimeManifestInvalid,StorageTreeError)):
                pinned.pin()
            assert not pinned.ready and trees.retained_trees == 1
            with pytest.raises(RuntimeManifestInvalid):
                pinned.read("python.exe", 0, 1)
        finally:
            pinned.close()


def test_directory_enumeration_is_bounded_even_for_valid_extra_names(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (_, trees, sid, root, parent, identity):
        for index in range(257):
            (root/f"member-{index}").touch()
        owner = trees.reserve(TreePurpose.PIN, sid)
        owner.root(parent, identity)
        try:
            with pytest.raises(StorageTreeError):
                owner.inventory(0)
        finally:
            owner.close()


def test_empty_inventory_can_restart_on_the_same_owned_handle(native, tmp_path_factory):
    with fixture(native, tmp_path_factory) as (_, trees, sid, _, parent, identity):
        owner = trees.reserve(TreePurpose.PIN, sid)
        owner.root(parent, identity)
        try:
            assert owner.inventory(0) == owner.inventory(0) == ()
        finally:
            owner.close()



@pytest.mark.parametrize("mode", range(1, 5))
def test_native_inventory_after_effect_and_error_paths_remain_incomplete(native, tmp_path_factory, mode):
    import subprocess
    with fixture(native, tmp_path_factory) as (_, _, _, root, _, _):
        (root/"member").write_bytes(b"member")
        executable = native[2]/"storage-tree-inventory-faults.exe"
        assert executable.is_file()
        result = subprocess.run([str(executable), str(root), str(mode)], capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout+result.stderr
        assert "incomplete enumeration never accepted" in result.stdout


def test_unknown_read_disables_ready_without_releasing_pins(native, tmp_path_factory, monkeypatch):
    with fixture(native, tmp_path_factory) as (_, trees, sid, root, parent, identity):
        manifest = installation(trees, sid, parent, identity, root)
        pinned = PinnedRuntime(trees, manifest.to_bytes(), manifest.sha256, sid).pin()
        original = trees._calls["tree_read"]
        def lose_return(*args):
            assert original(*args) == 0
            raise RuntimeError("lost read result")
        try:
            monkeypatch.setitem(trees._calls, "tree_read", lose_return)
            with pytest.raises(RuntimeError, match="lost read result"):
                pinned.read("python.exe", 0, 1)
            assert not pinned.ready and trees.retained_trees == 1
            with pytest.raises(AttributeError):
                pinned.manifest = manifest
        finally:
            pinned.close()
