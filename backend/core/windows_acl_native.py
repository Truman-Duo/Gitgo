"""Borrowed ACL handles; Rust owns handles, descriptor allocations and streams."""
import struct

from backend.core.native_sandbox import get_native, NativeBoundaryError


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


def observe_handles(handles, information):
    library = handles[0].library
    if any(handle.closed or handle.library is not library for handle in handles):
        raise NativeBoundaryError('ACL observations require live tokens from one module')
    operation = {0x1ff: 7, 1 | 2 | 4 | 16: 6}[information]
    offset, limit = 0, 64
    while offset < len(handles):
        batch = handles[offset:offset + limit]
        tokens = struct.pack('<' + 'Q' * len(batch), *(handle.token for handle in batch))
        try:
            raw = library.acl(operation, raw=tokens)
        except NativeBoundaryError as error:
            if error.errno != 122 or len(batch) == 1:
                raise
            limit = max(1, len(batch) // 2)
            continue
        rows, position = [], 0
        for _ in batch:
            if len(raw) - position < 28:
                library._sealed = True
                raise NativeBoundaryError('Native ACL observations are truncated')
            device = struct.unpack_from('<Q', raw, position)[0]
            inode = raw[position + 8:position + 24].hex()
            size = struct.unpack_from('<I', raw, position + 24)[0]
            position += 28
            if not 20 <= size <= 1024 * 1024 or size > len(raw) - position:
                library._sealed = True
                raise NativeBoundaryError('Native ACL observation size exceeds bounds')
            rows.append((device, inode, raw[position:position + size]))
            position += size
        if position != len(raw):
            library._sealed = True
            raise NativeBoundaryError('Native ACL observation count differs from its request')
        yield from rows
        offset += len(batch)


def write_descriptor(handle, raw):
    handle.library.acl(2, handle.token, raw)


def write_scoped_descriptor(handle, raw):
    handle.library.acl(5, handle.token, raw)


def descriptor_text(raw):
    return get_native().acl(3, raw=raw).decode('utf8')
