"""Complete native pins for an independently approved installed runtime."""
from pathlib import PurePosixPath
import os
import threading

from .native_windows_tree import TreePurpose, WindowsTreeLibrary, RuntimePins
from .runtime_manifest import MANIFEST_NAME, RuntimeManifest, require


class PinnedRuntime:
    """A single pin attempt; unknown close keeps its native owner retained.

    The caller supplies approval and retains the Host lifecycle lease. Neither
    this object nor a content hash creates either authority.
    """

    def __init__(self, library: WindowsTreeLibrary, raw: bytes, approved_digest: str, host_sid: str):
        self._pid,self._thread=os.getpid(),threading.get_ident()
        self._attempted = self._closed = self._ready = False
        self._owner = None
        self._library = library
        self._indices = {}
        self._manifest = RuntimeManifest.from_bytes(raw, approved_digest)
        self._raw = raw
        # The parent retains this wrapper before construction. Retain its leaf
        # before native reserve too, including an exception after that effect.
        self._owner = object.__new__(RuntimePins)
        RuntimePins.__init__(self._owner, library, TreePurpose.PIN, host_sid)

    def _require_identity(self):
        require(self._pid==os.getpid() and self._thread==threading.get_ident(),"runtime pin process/thread changed")

    @property
    def manifest(self):
        return self._manifest

    @property
    def ready(self):
        self._require_identity()
        return self._ready and not self._closed and self._owner is not None and not self._owner._closed and not self._owner._sealed

    def pin(self):
        self._require_identity()
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

    def _move_into(self, destination):
        """Transfer one existing leaf into a wrapper already retained by its parent.

        The destination begins disabled. The old wrapper loses readiness before
        either reference can move, and stays invalid after transfer. No native
        acquisition or duplicate code pin is hidden inside this operation.
        """
        require(type(destination) is PinnedRuntime and destination is not self
                and not hasattr(destination,"_owner"),"fresh retained destination wrapper required")
        self._require_identity()
        destination._pid,destination._thread=self._pid,self._thread
        destination._attempted=True
        destination._closed=destination._ready=False
        destination._owner=None
        destination._library=self._library
        require(self.ready,"complete live runtime pins required for transfer")
        destination._manifest=self._manifest
        destination._raw=self._raw
        destination._indices=dict(self._indices)
        self._ready=False
        destination._owner=self._owner
        self._owner=None
        self._closed=True
        destination._ready=True

    def close(self):
        if hasattr(self,"_pid"): self._require_identity()
        self._ready = False
        if not getattr(self,"_closed",False):
            owner = getattr(self,"_owner",None)
            cookie = getattr(owner,"_cookie",None)
            if owner is not None and (id(owner) in self._library._owners or cookie is not None and cookie.value):
                owner.close()
            self._closed = True
