"""Unicode Shell Link writer using IShellLinkW and IPersistFile.

Do not use WScript.Shell: its argument setter loses characters outside the
active ANSI code page, including combining characters.
"""
from __future__ import annotations

import ctypes as C
import os
from pathlib import Path
import uuid


def create_shortcut(path: Path, target: str | Path, arguments: str, icon: Path):
    if os.name != "nt":
        raise OSError("WINDOWS_SHELL_LINK_UNAVAILABLE")
    if any("\0" in value for value in (str(path), str(target), arguments, str(icon))):
        raise ValueError("WINDOWS_SHELL_LINK_INVALID_STRING")
    ole = C.WinDLL("ole32")
    ole.CoInitializeEx.argtypes = [C.c_void_p, C.c_ulong]
    ole.CoInitializeEx.restype = C.c_long
    ole.CoCreateInstance.argtypes = [C.c_void_p, C.c_void_p, C.c_ulong, C.c_void_p, C.POINTER(C.c_void_p)]
    ole.CoCreateInstance.restype = C.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    guid = lambda value: C.create_string_buffer(uuid.UUID(value).bytes_le)
    def checked(value):
        if value < 0:
            raise OSError("WINDOWS_SHELL_LINK_HRESULT_" + hex(value & 0xFFFFFFFF))
    def call(pointer, index, argtypes=(), values=(), result=C.c_long):
        table = C.cast(pointer, C.POINTER(C.POINTER(C.c_void_p))).contents
        function = C.WINFUNCTYPE(result, C.c_void_p, *argtypes)(table[index])
        return function(pointer, *values)
    initialized = ole.CoInitializeEx(None, 2)
    if initialized not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE keeps the caller's apartment
        checked(initialized)
    link, persist = C.c_void_p(), C.c_void_p()
    try:
        checked(ole.CoCreateInstance(guid("00021401-0000-0000-c000-000000000046"), None, 1,
                                    guid("000214f9-0000-0000-c000-000000000046"), C.byref(link)))
        checked(call(link, 20, (C.c_wchar_p,), (str(target),)))
        checked(call(link, 11, (C.c_wchar_p,), (arguments,)))
        checked(call(link, 17, (C.c_wchar_p, C.c_int), (str(icon), 0)))
        checked(call(link, 7, (C.c_wchar_p,), ("Gitgo Dashboard",)))
        checked(call(link, 15, (C.c_int,), (1,)))
        checked(call(link, 0, (C.c_void_p, C.POINTER(C.c_void_p)),
                     (guid("0000010b-0000-0000-c000-000000000046"), C.byref(persist))))
        checked(call(persist, 6, (C.c_wchar_p, C.c_int), (str(path), 1)))
    finally:
        if persist.value: call(persist, 2, result=C.c_ulong)
        if link.value: call(link, 2, result=C.c_ulong)
        if initialized in (0, 1): ole.CoUninitialize()
