"""Strict, bounded SemVer 2 precedence; build metadata never makes an upgrade."""

from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering
import re

from .validation import reject


_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


@total_ordering
@dataclass(frozen=True, eq=False)
class Version:
    core: tuple[int, int, int]
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> Version:
        if type(value) is not str or len(value) > 128:
            reject("UPDATE_VERSION_INVALID", "Invalid semantic version")
        match = _VERSION.fullmatch(value)
        if match is None:
            reject("UPDATE_VERSION_INVALID", "Expected a semantic version")
        pre = tuple(match.group(4).split(".")) if match.group(4) else ()
        if any(item.isdigit() and len(item) > 1 and item[0] == "0" for item in pre):
            reject("UPDATE_VERSION_INVALID", "Numeric prerelease identifiers cannot have leading zeros")
        return cls(tuple(int(match.group(index)) for index in (1, 2, 3)), pre)

    def __eq__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self.core == other.core and self.prerelease == other.prerelease

    def __hash__(self):
        return hash((self.core, self.prerelease))

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        if self.core != other.core:
            return self.core < other.core
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if left.isdigit() and right.isdigit():
                return int(left) < int(right)
            if left.isdigit() != right.isdigit():
                return left.isdigit()
            return left < right
        return len(self.prerelease) < len(other.prerelease)
