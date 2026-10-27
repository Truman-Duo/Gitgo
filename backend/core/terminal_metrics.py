"""Measure classic Windows console glyphs without changing its active buffer/font."""
import os
import sys
import threading
import unicodedata

_lock = threading.Lock()
_cache = {}


def console_display_metrics():
    if sys.platform != "win32":
        return None
    import ctypes as C
    from ctypes import wintypes as W

    class Coord(C.Structure):
        _fields_ = [("x", W.SHORT), ("y", W.SHORT)]
    class Rect(C.Structure):
        _fields_ = [("left", W.SHORT), ("top", W.SHORT), ("right", W.SHORT), ("bottom", W.SHORT)]
    class Info(C.Structure):
        _fields_ = [("size", Coord), ("cursor", Coord), ("attributes", W.WORD), ("window", Rect), ("maximum", Coord)]
    class Font(C.Structure):
        _fields_ = [("size", W.ULONG), ("index", W.DWORD), ("cell", Coord), ("family", W.UINT),
                    ("weight", W.UINT), ("face", W.WCHAR * 32)]

    kernel = C.WinDLL("kernel32", use_last_error=True)
    user = C.WinDLL("user32", use_last_error=True)
    kernel.GetConsoleWindow.restype = W.HWND
    kernel.AttachConsole.argtypes = [W.DWORD]
    kernel.GetConsoleProcessList.argtypes = [C.POINTER(W.DWORD), W.DWORD]
    user.IsWindowVisible.argtypes = [W.HWND]
    kernel.CreateConsoleScreenBuffer.argtypes = [W.DWORD, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p]
    kernel.CreateConsoleScreenBuffer.restype = W.HANDLE
    kernel.GetCurrentConsoleFontEx.argtypes = [W.HANDLE, W.BOOL, C.POINTER(Font)]
    kernel.SetConsoleCursorPosition.argtypes = [W.HANDLE, Coord]
    kernel.WriteConsoleW.argtypes = [W.HANDLE, W.LPCWSTR, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
    kernel.GetConsoleScreenBufferInfo.argtypes = [W.HANDLE, C.POINTER(Info)]
    kernel.CloseHandle.argtypes = [W.HANDLE]

    with _lock:
        attached, handle = False, None
        try:
            if not kernel.GetConsoleWindow():
                attached = bool(kernel.AttachConsole(0xFFFFFFFF))
                if not attached and C.get_last_error() == 5:
                    # Bun windowsHide may give the piped Host a private,
                    # windowless console. It still counts as attached, so
                    # AttachConsole fails with ACCESS_DENIED. Release only
                    # that unshared console, never an emulator/user console.
                    owners = (W.DWORD * 2)()
                    if (kernel.GetConsoleProcessList(owners, 2) == 1
                            and owners[0] == os.getpid() and kernel.FreeConsole()):
                        attached = bool(kernel.AttachConsole(0xFFFFFFFF))
            window = kernel.GetConsoleWindow()
            # ConPTY exposes an invisible pseudo window; its emulator owns
            # glyph widths. Only a visible classic console uses these metrics.
            if not window or not user.IsWindowVisible(window):
                return None
            handle = kernel.CreateConsoleScreenBuffer(0xC0000000, 3, None, 1, None)
            if handle in (None, C.c_void_p(-1).value):
                handle = None
                return None
            font = Font(size=C.sizeof(Font))
            if not kernel.GetCurrentConsoleFontEx(handle, False, C.byref(font)):
                return None
            key = (font.face, font.cell.x, font.cell.y, font.family, font.weight, kernel.GetConsoleOutputCP())
            if key in _cache:
                return {**_cache[key], "codepoint_widths": dict(_cache[key]["codepoint_widths"])}

            def width(char):
                written, info = W.DWORD(), Info()
                if (not kernel.SetConsoleCursorPosition(handle, Coord(0, 0))
                    or not kernel.WriteConsoleW(handle, char, 1, C.byref(written), None)
                    or written.value != 1 or not kernel.GetConsoleScreenBufferInfo(handle, C.byref(info))
                    or info.cursor.y != 0 or info.cursor.x not in (1, 2)):
                    raise OSError("Console glyph measurement unavailable")
                return info.cursor.x

            if width("A") != 1 or width("中") != 2:
                return None
            measured = {}
            # Assigned BMP ambiguous characters plus symbols/punctuation with
            # narrow Unicode width. Wide CJK and zero-width controls are left
            # to the existing Unicode/grapheme rules. Never activate this buffer.
            for codepoint in range(0xA0, 0x10000):
                char = chr(codepoint)
                category, asian = unicodedata.category(char), unicodedata.east_asian_width(char)
                if category[0] in "CM" or asian in {"W", "F"} or not (asian == "A" or category[0] in "SP"):
                    continue
                if width(char) == 2:
                    measured[str(codepoint)] = 2
            result = {"kind": "windows_console", "font": font.face, "codepoint_widths": measured}
            if len(_cache) >= 4:
                _cache.clear()
            _cache[key] = result
            return {**result, "codepoint_widths": dict(measured)}
        except OSError:
            return None
        finally:
            if handle is not None:
                kernel.CloseHandle(handle)
            if attached:
                kernel.FreeConsole()
