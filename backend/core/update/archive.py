"""Private, bounded extraction into a new owned staging directory, never install.

No extractall, ownership restoration, special files, or archive-controlled
cleanup paths. Unix links are created last, after validating the whole graph.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import struct
import tarfile
import time
import unicodedata
import zipfile
import zlib

from .transport import CHUNK_BYTES
from .validation import UpdateError, number, reject


@dataclass(frozen=True)
class ExtractionLimits:
    entries: int = 10000
    total_bytes: int = 4 * 1024**3
    file_bytes: int = 2 * 1024**3
    metadata_bytes: int = 8 * 1024**2
    expansion_ratio: int = 1000
    seconds: int = 300

    def __post_init__(self):
        for field, maximum in (("entries", 10000), ("total_bytes", 4 * 1024**3),
                               ("file_bytes", 2 * 1024**3), ("metadata_bytes", 8 * 1024**2),
                               ("expansion_ratio", 1000), ("seconds", 900)):
            number(getattr(self, field), maximum=maximum)


@dataclass(frozen=True)
class _Entry:
    name: str
    kind: str
    size: int
    executable: bool
    source: object
    link: str = ""


def _parts(name: str, *, link: bool = False) -> tuple[str, ...]:
    if (not isinstance(name, str) or not name or len(name.encode("utf-8", errors="replace")) > 1024
            or name.startswith("/") or "\\" in name
            or any(unicodedata.category(c).startswith("C") for c in name)):
        reject("UPDATE_ARCHIVE_PATH", "Unsafe archive path")
    parts = tuple(name.split("/"))
    if len(parts) > 32:
        reject("UPDATE_ARCHIVE_PATH", "Archive path is too deep")
    for part in parts:
        if link and part == "..":
            continue
        if (not part or part in (".", "..") or part.endswith((" ", "."))
                or any(c in part for c in '<>:"|?*') or len(part.encode("utf-8")) > 255
                or re.fullmatch(r"(?i)(con|prn|aux|nul|conin\$|conout\$|clock\$|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?", part)):
            reject("UPDATE_ARCHIVE_PATH", "Nonportable or reserved archive path")
    return parts


def _validate(entries: list[_Entry], limits: ExtractionLimits, checkpoint, *, wrapped: bool) -> str:
    nodes = {}
    canonical = {}
    roots = set()
    total = 0
    for item in entries:
        checkpoint()
        parts = _parts(item.name)
        roots.add(parts[0])
        if item.name in nodes:
            reject("UPDATE_ARCHIVE_DUPLICATE", "Archive repeats a path")
        for count in range(1, len(parts) + 1):
            name = "/".join(parts[:count])
            key = unicodedata.normalize("NFC", name).casefold()
            if key in canonical and canonical[key] != name:
                reject("UPDATE_ARCHIVE_COLLISION", "Archive paths collide on supported filesystems")
            canonical[key] = name
            if len(canonical) > limits.entries * 2:
                reject("UPDATE_ARCHIVE_LIMIT", "Archive creates too many implicit paths")
        nodes[item.name] = item
        number(item.size, minimum=0, maximum=limits.file_bytes)
        total += item.size
        if total > limits.total_bytes:
            reject("UPDATE_ARCHIVE_LIMIT", "Archive exceeds the unpacked size limit")
    if not entries or (wrapped and len(roots) != 1):
        reject("UPDATE_ARCHIVE_LAYOUT", "Expected a nonempty bundle with the target's archive layout")
    # Unix packagers wrap a single platform directory. Existing Windows ZIPs
    # contain product.json/gitgo.exe directly at archive root; do not invent a
    # wrapper requirement that would reject the project's own release format.
    root = next(iter(roots)) if wrapped else ""
    directories = {root} if wrapped else set()
    for item in entries:
        checkpoint()
        parts = item.name.split("/")
        for count in range(1, len(parts)):
            parent = "/".join(parts[:count])
            if parent in nodes and nodes[parent].kind != "directory":
                reject("UPDATE_ARCHIVE_PATH", "Archive writes through a file or link")
            directories.add(parent)
        if item.kind == "directory":
            directories.add(item.name)
    if wrapped and root in nodes and nodes[root].kind != "directory":
        reject("UPDATE_ARCHIVE_LAYOUT", "Bundle root must be a directory")
    if not any(item.kind == "file" for item in entries):
        reject("UPDATE_ARCHIVE_LAYOUT", "Bundle contains no regular files")
    for item in entries:
        checkpoint()
        if item.kind != "symlink":
            continue
        pending = list(_parts(item.link, link=True))
        resolved = item.name.split("/")[:-1]
        expansions = 0
        while pending:
            checkpoint()
            part = pending.pop(0)
            if part == "..":
                if len(resolved) <= 1:
                    reject("UPDATE_ARCHIVE_LINK", "Link escapes the bundle directory")
                resolved.pop()
                continue
            resolved.append(part)
            target = nodes.get("/".join(resolved))
            if target is not None and target.kind == "symlink":
                expansions += 1
                if expansions > 40:
                    reject("UPDATE_ARCHIVE_LINK", "Cyclic or excessively chained link")
                pending = list(_parts(target.link, link=True)) + pending
                resolved.pop()
            elif pending and "/".join(resolved) not in directories:
                reject("UPDATE_ARCHIVE_LINK", "Link traverses a missing directory or file")
        result = "/".join(resolved)
        if result not in nodes and result not in directories:
            reject("UPDATE_ARCHIVE_LINK", "Dangling archive link")
    return root


def _write_entries(entries, opener, destination, limits, checkpoint, *, wrapped):
    root = _validate(entries, limits, checkpoint, wrapped=wrapped)
    # No symlinks exist in the fresh payload while any regular file is written.
    for item in entries:
        checkpoint()
        target = destination / item.name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if item.kind == "directory":
            target.mkdir(exist_ok=True, mode=0o700)
        elif item.kind == "file":
            with opener(item.source) as source, target.open("xb") as output:
                received = 0
                while chunk := source.read(CHUNK_BYTES):
                    checkpoint()
                    received += len(chunk)
                    if received > item.size:
                        reject("UPDATE_ARCHIVE_SIZE", "Expanded file exceeds its declaration")
                    output.write(chunk)
                if received != item.size:
                    reject("UPDATE_ARCHIVE_SIZE", "Expanded file was truncated")
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o700 if item.executable else 0o600)
    for item in entries:
        checkpoint()
        if item.kind == "symlink":
            target = destination / item.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.symlink_to(item.link, target_is_directory=False)
    return destination / root


class _BoundedTarInfo(tarfile.TarInfo):
    """Reject oversized extension records before stdlib assembles their bodies.

    New Python versions can assemble one body from several smaller reads, so
    limiting an individual file-object read does not bound one PAX/GNU record.
    """

    def _proc_pax(self, archive):
        if self.size > 1024**2:
            reject("UPDATE_ARCHIVE_LIMIT", "Tar extension exceeds its limit")
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        if self.size > 1024**2:
            reject("UPDATE_ARCHIVE_LIMIT", "Tar extension exceeds its limit")
        return super()._proc_gnulong(archive)


class _TarReader:
    """Bound tarfile's individual metadata reads before it allocates PAX bodies."""

    def __init__(self, source, limit):
        self.source, self.limit = source, limit
        self.metadata_phase = True
        self.metadata_read = 0

    def read(self, size=-1):
        if size < 0 or size > 1024**2:
            reject("UPDATE_ARCHIVE_LIMIT", "Tar metadata read exceeds its limit")
        if self.metadata_phase:
            self.metadata_read += size
            if self.metadata_read > self.limit:
                reject("UPDATE_ARCHIVE_LIMIT", "Tar aggregate metadata exceeds its limit")
        return self.source.read(size)

    def seek(self, *args):
        return self.source.seek(*args)

    def tell(self):
        return self.source.tell()


def _extract_tar(source, destination, work, limits, checkpoint):
    # Inflating first bounds hidden PAX/GNU headers as well as regular files;
    # Incremental zlib validates gzip's footer/CRC without gzip.GzipFile's
    # uncheckpointed optional-header reads or automatic concatenated members.
    plain = work / "expanded.tar"
    maximum = min(limits.total_bytes + limits.metadata_bytes,
                  os.fstat(source.fileno()).st_size * limits.expansion_ratio)
    compressed = zlib.decompressobj(wbits=31)
    with plain.open("xb") as output:
        size = 0
        while raw := source.read(CHUNK_BYTES):
            checkpoint()
            if compressed.eof:
                reject("UPDATE_ARCHIVE_INVALID", "Trailing or concatenated gzip members are unsupported")
            while raw:
                checkpoint()
                chunk = compressed.decompress(raw, CHUNK_BYTES)
                raw = compressed.unconsumed_tail
                if compressed.unused_data:
                    reject("UPDATE_ARCHIVE_INVALID", "Trailing or concatenated gzip members are unsupported")
                size += len(chunk)
                if size > maximum:
                    reject("UPDATE_ARCHIVE_LIMIT", "Tar expansion exceeds its limit")
                output.write(chunk)
        if not compressed.eof:
            reject("UPDATE_ARCHIVE_INVALID", "Gzip member was truncated")
    with plain.open("rb") as handle:
        reader = _TarReader(handle, limits.metadata_bytes)
        with tarfile.open(fileobj=reader, mode="r:", tarinfo=_BoundedTarInfo) as archive:
            return _read_tar_entries(archive, reader, destination, limits, checkpoint)


def _read_tar_entries(archive, reader, destination, limits, checkpoint):
    entries = []
    for member in archive:
        checkpoint()
        if len(entries) >= limits.entries:
            reject("UPDATE_ARCHIVE_LIMIT", "Archive contains too many entries")
        # BSD tar emits provenance xattrs even with COPYFILE_DISABLE. They are
        # bounded metadata, not special files, and are NEVER restored. Keep
        # rejecting sparse/type-changing extensions instead of rejecting the
        # project's normal macOS archive solely because it has metadata.
        unsafe_extensions = any(k.startswith("GNU.sparse") or
                                (k.startswith("SCHILY") and not k.startswith(
                                    ("SCHILY.xattr.", "SCHILY.acl.", "SCHILY.fflags")))
                                for k in member.pax_headers)
        if member.sparse is not None or unsafe_extensions:
            reject("UPDATE_ARCHIVE_TYPE", "Sparse or type-changing tar entries are unsupported")
        if member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
            kind = "file"
        elif member.isdir():
            kind = "directory"
        elif member.issym():
            kind = "symlink"
        else:
            reject("UPDATE_ARCHIVE_TYPE", "Only files, directories and internal Unix symlinks are supported")
        if kind != "file" and member.size != 0:
            reject("UPDATE_ARCHIVE_SIZE", "Non-file tar entry carries data")
        entries.append(_Entry(member.name, kind, member.size, bool(member.mode & 0o111), member, member.linkname))
    reader.metadata_phase = False
    # The caller removes the tar spool after closing its descriptor (Windows).
    return _write_entries(entries, archive.extractfile, destination, limits, checkpoint, wrapped=True)


def _zip_directory_guard(source, limits):
    # ZIP's central directory is loaded eagerly by zipfile. Bound it BEFORE
    # constructing ZipFile; reject multi-disk and ZIP64 in this first format.
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < 22:
        reject("UPDATE_ARCHIVE_INVALID", "ZIP end record is missing")
    _, disk, start_disk, count_disk, count, directory_size, offset, comment = struct.unpack("<4s4H2LH", tail[at:at + 22])
    eocd = size - len(tail) + at
    if (disk or start_disk or count_disk != count or count == 65535
            or offset == 0xffffffff or directory_size == 0xffffffff
            or count > limits.entries or directory_size > limits.metadata_bytes
            or len(tail) - at != 22 + comment or offset + directory_size != eocd):
        reject("UPDATE_ARCHIVE_LIMIT", "ZIP directory is unsupported or exceeds its limits")
    source.seek(offset)
    directory = source.read(directory_size)
    position = actual = 0
    while position < len(directory):
        if position + 46 > len(directory) or directory[position:position + 4] != b"PK\x01\x02":
            reject("UPDATE_ARCHIVE_INVALID", "Invalid ZIP central directory")
        name, extra, comment = struct.unpack_from("<3H", directory, position + 28)
        position += 46 + name + extra + comment
        actual += 1
        if actual > limits.entries or position > len(directory):
            reject("UPDATE_ARCHIVE_LIMIT", "ZIP central directory entries exceed limits")
    if actual != count:
        reject("UPDATE_ARCHIVE_INVALID", "ZIP directory count is inconsistent")
    source.seek(0)


def _extract_zip(source, destination, limits, checkpoint):
    _zip_directory_guard(source, limits)
    with zipfile.ZipFile(source) as archive:
        entries = []
        for member in archive.infolist():
            checkpoint()
            mode = member.external_attr >> 16
            filetype = stat.S_IFMT(mode)
            if (member.flag_bits & (1 | 64) or member.extract_version > 20
                    or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or filetype not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or member.orig_filename != member.filename):
                reject("UPDATE_ARCHIVE_TYPE", "Encrypted, linked, special or unsupported ZIP entry")
            kind = "directory" if member.is_dir() else "file"
            if filetype == stat.S_IFDIR and kind != "directory":
                reject("UPDATE_ARCHIVE_TYPE", "ZIP directory metadata is inconsistent")
            if kind == "directory" and member.file_size:
                reject("UPDATE_ARCHIVE_SIZE", "ZIP directory carries data")
            if member.file_size > max(1, member.compress_size) * limits.expansion_ratio:
                reject("UPDATE_ARCHIVE_LIMIT", "ZIP entry expansion exceeds its limit")
            entries.append(_Entry(member.filename[:-1] if kind == "directory" else member.filename,
                                  kind, member.file_size, bool(mode & 0o111), member))
        if len(entries) > limits.entries:
            reject("UPDATE_ARCHIVE_LIMIT", "Archive contains too many entries")
        return _write_entries(entries, archive.open, destination, limits, checkpoint, wrapped=False)


def _extract_archive(source, archive_format, destination: Path, work: Path,
                     limits: ExtractionLimits, cancelled) -> Path:
    started = time.monotonic()

    def checkpoint():
        if cancelled():
            reject("UPDATE_CANCELLED", "Update preparation was cancelled")
        if time.monotonic() - started >= limits.seconds:
            reject("UPDATE_ARCHIVE_TIMEOUT", "Archive processing exceeded its deadline")

    try:
        checkpoint()
        source.seek(0)
        destination.mkdir(mode=0o700)
        if archive_format == "tar.gz":
            bundle = _extract_tar(source, destination, work, limits, checkpoint)
            (work / "expanded.tar").unlink()
            return bundle
        if archive_format == "zip":
            return _extract_zip(source, destination, limits, checkpoint)
        reject("UPDATE_ARCHIVE_TYPE", "Unsupported archive format")
    except UpdateError:
        raise
    except Exception:
        raise UpdateError("UPDATE_ARCHIVE_INVALID", "Archive could not be safely expanded") from None
