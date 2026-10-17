"""Complete native pins for an independently approved installed runtime."""
from pathlib import PurePosixPath

from .native_windows_tree import TreePurpose, WindowsTreeLibrary
from .runtime_manifest import MANIFEST_NAME, RuntimeManifest, require


class PinnedRuntime:
    """A single pin attempt; unknown close keeps its native owner retained.

    The caller supplies approval and retains the Host lifecycle lease. Neither
    this object nor a content hash creates either authority.
    """

    def __init__(self, library: WindowsTreeLibrary, raw: bytes, approved_digest: str, host_sid: str):
        self._manifest = RuntimeManifest.from_bytes(raw, approved_digest)
        self._raw = raw
        self._owner = library.reserve(TreePurpose.PIN, host_sid)
        self._attempted = self._closed = self._ready = False
        self._indices = {}

    @property
    def manifest(self):
        return self._manifest

    @property
    def ready(self):
        return self._ready and not self._closed

    def pin(self):
        require(not self._attempted and not self._closed, "runtime pin attempt already consumed")
        self._attempted = True
        self._owner.root_path(self.manifest.root, self.manifest.root_identity)
        content = self.manifest.content
        files = {item.path: (item.length, item.sha256) for item in content.files}
        files[MANIFEST_NAME] = (len(self._raw), self.manifest.sha256)
        directories = {"": 0}
        for directory in ("", *content.directories):
            index = directories[directory]
            children = {PurePosixPath(name).name: name for name in (*content.directories, *files)
                        if str(PurePosixPath(name).parent) == (directory or ".")}
            observed = self._owner.inventory(index)
            require(len(observed) == len(set(observed)) and set(observed) == set(children),
                    "incomplete or unexpected runtime directory membership")
            for leaf in sorted(observed):
                name = children[leaf]
                if name in files:
                    length, expected = files[name]
                    member = self._owner.add(index, leaf, length=length)
                    require(self._owner.digest(member) == expected, "runtime member content changed")
                    self._indices[name] = member
                else:
                    directories[name] = self._owner.add(index, leaf, directory=True)
        require(set(self._indices) == set(files), "runtime member pins are incomplete")
        self._ready = True
        return self

    def read(self, name, offset, size):
        require(self.ready and type(name) is str and name in self._indices, "live installed runtime member required")
        self._ready = False
        result = self._owner.read(self._indices[name], offset, size)
        self._ready = True
        return result

    def close(self):
        self._ready = False
        if not self._closed:
            self._owner.close()
            self._closed = True
