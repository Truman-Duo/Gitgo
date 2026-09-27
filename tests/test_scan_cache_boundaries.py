from __future__ import annotations

import os
from pathlib import Path

from backend.adapters.local_file_adapter import LocalFileAdapter
from backend.core.cache import FileHashCache
from backend.core.operations.scan import compare_files


def test_normalized_hash_cache_is_namespaced_by_repository(tmp_path_factory: Path):
    workspace = tmp_path_factory / "workspace"
    release = tmp_path_factory / "release"
    workspace.mkdir()
    release.mkdir()
    (workspace / "same-name.txt").write_text("left\n", encoding="utf-8")
    (release / "same-name.txt").write_text("right", encoding="utf-8")
    # Same size and timestamp used to be capable of colliding when workspace
    # and release shared a relative-path-only cache key.
    stamp = 1_700_000_000
    os.utime(workspace / "same-name.txt", (stamp, stamp))
    os.utime(release / "same-name.txt", (stamp, stamp))
    cache = FileHashCache(tmp_path_factory / "cache")

    first = compare_files(
        workspace, release, ["same-name.txt"],
        ws_adapter=LocalFileAdapter(workspace),
        bk_adapter=LocalFileAdapter(release),
        normalize_eol=True, hash_cache=cache,
    )
    first_hits = cache.stats()["hits"]
    second = compare_files(
        workspace, release, ["same-name.txt"],
        ws_adapter=LocalFileAdapter(workspace),
        bk_adapter=LocalFileAdapter(release),
        normalize_eol=True, hash_cache=cache,
    )

    assert first[0].status == "modified"
    assert second[0].status == "modified"
    assert cache.stats()["hits"] > first_hits


def test_incremental_compare_does_not_walk_the_release_tree(tmp_path_factory: Path):
    workspace = tmp_path_factory / "workspace"
    release = tmp_path_factory / "release"
    workspace.mkdir()
    release.mkdir()
    (workspace / "changed.txt").write_text("new", encoding="utf-8")
    (release / "changed.txt").write_text("old", encoding="utf-8")

    class NoWalkRelease(LocalFileAdapter):
        def walk(self, path=""):
            raise AssertionError("incremental comparison walked the release tree")

    entries = compare_files(
        workspace, release, ["changed.txt"],
        ws_adapter=LocalFileAdapter(workspace),
        bk_adapter=NoWalkRelease(release),
        normalize_eol=True, hash_cache=FileHashCache(tmp_path_factory / "cache"),
        detect_renames=False,
    )

    assert entries[0].status == "modified"
