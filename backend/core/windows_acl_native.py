"""Borrowed ACL handles; Rust owns handles, descriptor allocations and streams."""
from backend.core.native_sandbox import get_native


class AclHandle(int):
    def __new__(cls, raw, token, library):
        value = super().__new__(cls, raw)
        value.token, value.library, value.closed = token, library, False
        return value

    def close(self):
        if not self.closed:
            self.library.call(op='acl_release', token=self.token)
            self.closed = True


def open_file(path, access):
    library = get_native()
    value = library.call(op='acl_open', path=str(path), access=access)
    return AclHandle(value['handle'], value['token'], library)


def read_descriptor(handle, information):
    operations = {0x1ff: 1, 1 | 2 | 4 | 16: 4}
    return handle.library.acl(operations[information], handle.token)


def write_descriptor(handle, raw):
    handle.library.acl(2, handle.token, raw)


def write_scoped_descriptor(handle, raw):
    handle.library.acl(5, handle.token, raw)


def descriptor_text(raw):
    return get_native().acl(3, raw=raw).decode('utf8')
