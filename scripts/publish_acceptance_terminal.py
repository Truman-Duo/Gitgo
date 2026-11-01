"""Drive the real Windows console through Issue 40 integration and packaging.

All repositories, native runtimes, exports and screen buffers are disposable
children of the explicitly supplied Desktop acceptance directory. The product
sources come from this checkout; alpha3-work is used only for tool binaries.
"""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib


class COORD(C.Structure):
    _fields_ = [("X", W.SHORT), ("Y", W.SHORT)]


class RECT(C.Structure):
    _fields_ = [("Left", W.SHORT), ("Top", W.SHORT), ("Right", W.SHORT), ("Bottom", W.SHORT)]


class INFO(C.Structure):
    _fields_ = [("size", COORD), ("cursor", COORD), ("attributes", W.WORD), ("window", RECT), ("maximum", COORD)]


class CHAR(C.Union):
    _fields_ = [("UnicodeChar", W.WCHAR), ("AsciiChar", W.CHAR)]


class KEY(C.Structure):
    _fields_ = [("down", W.BOOL), ("repeat", W.WORD), ("virtual", W.WORD), ("scan", W.WORD), ("char", CHAR), ("control", W.DWORD)]


class EVENT(C.Union):
    _fields_ = [("key", KEY), ("padding", C.c_byte * 16)]


class INPUT(C.Structure):
    _fields_ = [("type", W.WORD), ("event", EVENT)]


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
                ("image_size", W.DWORD), ("x_ppm", W.LONG), ("y_ppm", W.LONG),
                ("colors", W.DWORD), ("important", W.DWORD)]


class PROCESSENTRY(C.Structure):
    _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD), ("executable", W.WCHAR * 260)]


class FONTINFO(C.Structure):
    _fields_ = [("size", W.ULONG), ("index", W.DWORD), ("font_size", COORD),
                ("family", W.UINT), ("weight", W.UINT), ("face", W.WCHAR * 32)]


class CHARINFO(C.Structure):
    _fields_ = [("char", CHAR), ("attributes", W.WORD)]


def console_client(process):
    """Find only the Python client created by our explicit classic conhost."""
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes, kernel.CreateToolhelp32Snapshot.restype = [W.DWORD, W.DWORD], W.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot in {None, C.c_void_p(-1).value}:
            raise C.WinError(C.get_last_error())
        try:
            row = PROCESSENTRY()
            row.size = C.sizeof(PROCESSENTRY)
            more = kernel.Process32FirstW(snapshot, C.byref(row))
            while more:
                if row.parent == process.pid and row.executable.casefold() == "python.exe":
                    return row.pid
                more = kernel.Process32NextW(snapshot, C.byref(row))
        finally:
            kernel.CloseHandle(snapshot)
        if process.poll() is not None:
            raise RuntimeError("Owned console host exited before creating its Python client")
        time.sleep(.15)
    raise RuntimeError("Owned console Python client was not found")


def git(repo, *args):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith(("GIT_", "GCM_"))}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    result = subprocess.run(["git", "-C", str(repo), "-c", "core.hooksPath=" + str(repo.parent / "empty-hooks"),
        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True, encoding="utf8",
        creationflags=subprocess.CREATE_NO_WINDOW, env=environment)
    return result.stdout.strip()


def copy_runtime(source, root):
    runtime = root / "runtime"
    runtime.mkdir()
    base = source / "alpha3-work/tools/python-review-clean"
    for pattern in ("python.exe", "*.dll", "*.pyd", "python314.zip"):
        for entry in base.glob(pattern):
            shutil.copy2(entry, runtime / entry.name)
    (runtime / "python314._pth").write_text("python314.zip\n.\n", encoding="utf8")
    from tests.native_source_runtime import populate_source_runtime
    populate_source_runtime(runtime)
    for name in ("watchdog", "rich", "markdown_it", "mdurl", "pytest", "_pytest", "pluggy", "packaging", "iniconfig", "pygments", "colorama", "py"):
        spec = importlib.util.find_spec(name)
        if spec is None:
            continue
        if spec.submodule_search_locations:
            shutil.copytree(next(iter(spec.submodule_search_locations)), runtime / name,
                dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(spec.origin, runtime / Path(spec.origin).name)
    return runtime


def fixtures(root):
    workspace, trial, export = root / "workspace", root / "trial", root / "export"
    workspace.mkdir()
    export.mkdir()
    (root / "empty-hooks").mkdir()
    git(workspace, "init", "-b", "main")
    git(workspace, "config", "user.name", "Publish Terminal Acceptance")
    git(workspace, "config", "user.email", "terminal@gitgo.invalid")
    (workspace / "test_release.py").write_text("from pathlib import Path\n\ndef test_combined():\n"
        "    assert Path('workspace.txt').read_text() == 'workspace contribution\\n'\n"
        "    assert Path('trial.txt').read_text() == 'trial contribution\\n'\n", encoding="utf8", newline="\n")
    (workspace / "build_release.py").write_text("from pathlib import Path\n"
        "data = Path('workspace.txt').read_bytes() + Path('trial.txt').read_bytes()\n"
        "Path('dist').mkdir()\nPath('dist/release.txt').write_bytes(data)\nprint('built combined artifact')\n", encoding="utf8", newline="\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "[TERM-1] feat: acceptance baseline")
    baseline = git(workspace, "rev-parse", "HEAD")
    git(workspace, "branch", "formal", baseline)
    git(root, "clone", "--no-hardlinks", str(workspace), str(trial))
    git(trial, "config", "user.name", "Publish Trial Acceptance")
    git(trial, "config", "user.email", "trial@gitgo.invalid")
    (trial / "trial.txt").write_text("trial contribution\n", encoding="utf8", newline="\n")
    git(trial, "add", "trial.txt")
    git(trial, "commit", "-m", "[TERM-2] feat: incoming contribution")
    (workspace / "workspace.txt").write_text("workspace contribution\n", encoding="utf8", newline="\n")
    git(workspace, "add", "workspace.txt")
    git(workspace, "commit", "-m", "[TERM-3] feat: workspace contribution")
    (workspace / "uncommitted.txt").write_text("keep untouched\n", encoding="utf8")
    return {"workspace": workspace, "trial": trial, "export": export, "baseline": baseline,
            "candidate": git(workspace, "rev-parse", "HEAD"), "trial_oid": git(trial, "rev-parse", "HEAD"),
            "head_ref": git(workspace, "symbolic-ref", "HEAD"), "dirty": git(workspace, "status", "--porcelain")}


class Console:
    def __init__(self, process, root, client_pid, *, visible=False):
        self.process, self.root, self.phases, self.window = process, root, {}, None
        self.visible = visible
        self.input_latencies = []
        self.resize_modes = []
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.user = C.WinDLL("user32", use_last_error=True)
        self.user.SetProcessDPIAware()
        self.kernel.CreateFileW.restype = W.HANDLE
        self.kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
        self.kernel.GetConsoleScreenBufferInfo.argtypes = [W.HANDLE, C.POINTER(INFO)]
        self.kernel.ReadConsoleOutputCharacterW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, COORD, C.POINTER(W.DWORD)]
        self.kernel.ReadConsoleOutputAttribute.argtypes = [W.HANDLE, C.POINTER(W.WORD), W.DWORD, COORD, C.POINTER(W.DWORD)]
        self.kernel.WriteConsoleInputW.argtypes = [W.HANDLE, C.POINTER(INPUT), W.DWORD, C.POINTER(W.DWORD)]
        self.kernel.SetConsoleScreenBufferSize.argtypes = [W.HANDLE, COORD]
        self.kernel.SetConsoleWindowInfo.argtypes = [W.HANDLE, W.BOOL, C.POINTER(RECT)]
        self.kernel.GetConsoleWindow.restype = W.HWND
        self.kernel.SetConsoleTitleW.argtypes = [W.LPCWSTR]
        self.user.IsIconic.argtypes = [W.HWND]
        self.user.ShowWindow.argtypes = [W.HWND, C.c_int]
        self.user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        time.sleep(.15)
        self.kernel.FreeConsole()
        if not self.kernel.AttachConsole(client_pid):
            raise C.WinError(C.get_last_error())
        self.window = self.kernel.GetConsoleWindow()
        self.kernel.SetConsoleTitleW("Gitgo Issue 40 Acceptance - " + root.name)
        self.input = self.kernel.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
        self.output = self.kernel.CreateFileW("CONOUT$", 0xC0000000, 3, None, 3, 0, None)
        self.initial_console_modes = self.modes()

    def modes(self):
        self.kernel.GetConsoleMode.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
        result = {}
        for name, handle in (("input", self.input), ("output", self.output)):
            mode = W.DWORD()
            result[name] = mode.value if self.kernel.GetConsoleMode(handle, C.byref(mode)) else None
        return result

    def screen(self):
        info = INFO()
        if not self.kernel.GetConsoleScreenBufferInfo(self.output, C.byref(info)):
            raise C.WinError(C.get_last_error())
        lines = []
        for row in range(info.window.Top, info.window.Bottom + 1):
            buffer, count = C.create_unicode_buffer(info.size.X + 1), W.DWORD()
            if not self.kernel.ReadConsoleOutputCharacterW(self.output, buffer, info.size.X, COORD(0, row), C.byref(count)):
                raise C.WinError(C.get_last_error())
            lines.append(buffer.value.rstrip())
        return "\n".join(lines), info.window.Right - info.window.Left + 1, info.window.Bottom - info.window.Top + 1

    def capture(self, name):
        value, width, height = self.screen()
        info = INFO()
        if not self.kernel.GetConsoleScreenBufferInfo(self.output, C.byref(info)):
            raise C.WinError(C.get_last_error())
        attributes, count = (W.WORD * (width * height))(), W.DWORD()
        if not self.kernel.ReadConsoleOutputAttribute(self.output, attributes, width * height,
                                                      COORD(0, info.window.Top), C.byref(count)):
            raise C.WinError(C.get_last_error())
        colors = sorted({int(value) & 255 for value in attributes[:count.value]})
        self.phases[name] = {"text": value, "width": width, "height": height, "color_attributes": colors,
                             "monotonic": time.monotonic(), "console_metrics": self.metrics()}
        (self.root / "terminal-phases.json").write_text(json.dumps(self.phases, ensure_ascii=False, indent=2), encoding="utf8")
        (self.root / (name + ".txt")).write_text(value, encoding="utf8")
        print(name, flush=True)
        return value

    def metrics(self):
        self.kernel.GetCurrentConsoleFontEx.argtypes = [W.HANDLE, W.BOOL, C.POINTER(FONTINFO)]
        self.kernel.ReadConsoleOutputW.argtypes = [W.HANDLE, C.POINTER(CHARINFO), COORD, COORD, C.POINTER(RECT)]
        font, info = FONTINFO(), INFO()
        font.size = C.sizeof(FONTINFO)
        if not self.kernel.GetCurrentConsoleFontEx(self.output, False, C.byref(font)):
            raise C.WinError(C.get_last_error())
        if not self.kernel.GetConsoleScreenBufferInfo(self.output, C.byref(info)):
            raise C.WinError(C.get_last_error())
        width, height = info.window.Right - info.window.Left + 1, info.window.Bottom - info.window.Top + 1
        cells = (CHARINFO * (width * height))()
        rectangle = RECT(info.window.Left, info.window.Top, info.window.Right, info.window.Bottom)
        if not self.kernel.ReadConsoleOutputW(self.output, cells, COORD(width, height), COORD(0, 0), C.byref(rectangle)):
            raise C.WinError(C.get_last_error())
        groups = {"ambiguous": [], "cjk": [], "ascii": []}
        for index, cell in enumerate(cells):
            char, flags = cell.char.UnicodeChar, int(cell.attributes) & 0x300
            group = "ambiguous" if char in "·•→←↑↓›─│" else "cjk" if 0x3400 <= ord(char) <= 0x9fff else "ascii" if char.isascii() and char.isalnum() else None
            if group and len(groups[group]) < (128 if group == "ambiguous" else 16):
                groups[group].append({"row": index // width, "column": index % width, "character": char, "lvb_flags": flags})
        return {"output_codepage": self.kernel.GetConsoleOutputCP(), "input_codepage": self.kernel.GetConsoleCP(),
                "font": font.face, "font_size": [font.font_size.X, font.font_size.Y],
                "buffer_size": [info.size.X, info.size.Y], "window_size": [width, height], "console_modes": self.modes(), **groups}

    def probe_widths(self):
        kernel = self.kernel
        kernel.CreateConsoleScreenBuffer.argtypes = [W.DWORD, W.DWORD, C.c_void_p, W.DWORD, C.c_void_p]
        kernel.CreateConsoleScreenBuffer.restype = W.HANDLE
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel.SetConsoleCursorPosition.argtypes = [W.HANDLE, COORD]
        kernel.WriteConsoleW.argtypes = [W.HANDLE, W.LPCWSTR, W.DWORD, C.POINTER(W.DWORD), C.c_void_p]
        kernel.GetCurrentConsoleFontEx.argtypes = [W.HANDLE, W.BOOL, C.POINTER(FONTINFO)]
        private = kernel.CreateConsoleScreenBuffer(0xC0000000, 3, None, 1, None)
        if private in {None, C.c_void_p(-1).value}:
            raise C.WinError(C.get_last_error())
        try:
            font = FONTINFO()
            font.size = C.sizeof(FONTINFO)
            if not kernel.GetCurrentConsoleFontEx(private, False, C.byref(font)):
                raise C.WinError(C.get_last_error())
            widths = {}
            for char in ("·", "→", "─", "中", "A"):
                if not kernel.SetConsoleCursorPosition(private, COORD(0, 0)):
                    raise C.WinError(C.get_last_error())
                count, info = W.DWORD(), INFO()
                if not kernel.WriteConsoleW(private, char, 1, C.byref(count), None) or count.value != 1:
                    raise C.WinError(C.get_last_error())
                if not kernel.GetConsoleScreenBufferInfo(private, C.byref(info)):
                    raise C.WinError(C.get_last_error())
                widths[char] = info.cursor.X
            return {"inactive_buffer": True, "font": font.face, "font_size": [font.font_size.X, font.font_size.Y], "widths": widths}
        finally:
            kernel.CloseHandle(private)

    def key(self, char="", virtual=0, control=0):
        if char and ord(char) > 0xFFFF:
            raw = char.encode("utf-16-le")
            for index in range(0, len(raw), 2):
                self.key(chr(int.from_bytes(raw[index:index + 2], "little")), virtual, control)
            return
        event, count = INPUT(), W.DWORD()
        event.type = 1
        event.event.key.down, event.event.key.repeat = True, 1
        event.event.key.char.UnicodeChar = char or "\0"
        event.event.key.virtual, event.event.key.control = virtual, control
        if not self.kernel.WriteConsoleInputW(self.input, C.byref(event), 1, C.byref(count)):
            raise C.WinError(C.get_last_error())

    def send(self, value):
        for char in value:
            self.key(char, {"\r": 13, "\x1b": 27, "\x15": 85}.get(char, 0), 8 if char == "\x15" else 0)
        time.sleep(.3)

    def command(self, value):
        self.send("/")
        self.send(value)
        self.send("\r")

    def field(self, value):
        self.send("\x15")
        if value and len(value) <= 40:
            before = self.screen()[0]
            started, observed = time.monotonic(), False
            for char in value:
                self.key(char)
            deadline = started + 2
            while time.monotonic() < deadline:
                current = self.screen()[0]
                # ReadConsoleOutputCharacterW represents rendered non-BMP
                # glyphs as U+FFFD; the saved backend value is checked exactly.
                observed_value = "".join("\ufffd" if ord(char) > 0xFFFF else char for char in value)
                if current != before and any(line.strip() == observed_value for line in current.splitlines()):
                    self.input_latencies.append({"value": value, "visible_after_ms": round((time.monotonic() - started) * 1000, 2)})
                    observed = True
                    break
                time.sleep(.01)
            if not observed:
                self.input_latencies.append({"value": value, "observed": False})
            time.sleep(.1)
        else:
            self.send(value)
        self.send("\r")

    def wait(self, needle, phase, timeout=35):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle in self.screen()[0]:
                return self.capture(phase)
            if self.process.poll() is not None:
                raise RuntimeError("Native terminal launcher exited")
            time.sleep(.25)
        self.capture(phase + "-failed")
        raise RuntimeError("Terminal phase unavailable: " + phase)

    def resize(self, width, height):
        # The VT alternate buffer follows the real window. Resizing its old
        # backing buffer can succeed without changing the active viewport.
        self.user.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        self.user.GetDpiForWindow.argtypes = [W.HWND]
        self.user.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
        self.kernel.GetCurrentConsoleFontEx.argtypes = [W.HANDLE, W.BOOL, C.POINTER(FONTINFO)]
        if self.visible and self.user.IsIconic(self.window):
            self.user.ShowWindow(self.window, 9)
            time.sleep(.5)
        for _ in range(5):
            _, actual_width, actual_height = self.screen()
            if (actual_width, actual_height) == (width, height):
                self.resize_modes.append({"width": width, "height": height, "modes": self.modes()})
                return
            rect, font = W.RECT(), FONTINFO()
            font.size = C.sizeof(FONTINFO)
            if not self.user.GetWindowRect(self.window, C.byref(rect)) or not self.kernel.GetCurrentConsoleFontEx(self.output, False, C.byref(font)):
                raise C.WinError(C.get_last_error())
            scale = self.user.GetDpiForWindow(self.window) / 96
            pixels_x = round(rect.right - rect.left + (width - actual_width) * font.font_size.X * scale)
            pixels_y = round(rect.bottom - rect.top + (height - actual_height) * font.font_size.Y * scale)
            if not self.user.SetWindowPos(self.window, None, 0, 0, pixels_x, pixels_y, 0x16):
                raise C.WinError(C.get_last_error())
            time.sleep(.7)
        raise RuntimeError(f"Console resize did not settle at {width}x{height}")

    def screenshot(self, name):
        """Capture actual visible pixels of this driver's own console window."""
        user, gdi = self.user, C.WinDLL("gdi32", use_last_error=True)
        user.IsWindowVisible.argtypes = [W.HWND]
        user.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
        user.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
        user.GetDC.argtypes, user.GetDC.restype = [W.HWND], W.HDC
        user.ReleaseDC.argtypes = [W.HWND, W.HDC]
        gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [W.HDC], W.HDC
        gdi.CreateCompatibleBitmap.argtypes, gdi.CreateCompatibleBitmap.restype = [W.HDC, C.c_int, C.c_int], W.HBITMAP
        gdi.SelectObject.argtypes, gdi.SelectObject.restype = [W.HDC, W.HANDLE], W.HANDLE
        gdi.BitBlt.argtypes = [W.HDC, C.c_int, C.c_int, C.c_int, C.c_int, W.HDC, C.c_int, C.c_int, W.DWORD]
        gdi.GetDIBits.argtypes = [W.HDC, W.HBITMAP, W.UINT, W.UINT, C.c_void_p, C.POINTER(BITMAPINFOHEADER), W.UINT]
        gdi.DeleteObject.argtypes, gdi.DeleteDC.argtypes = [W.HANDLE], [W.HDC]
        if user.IsIconic(self.window):
            user.ShowWindow(self.window, 9)
            time.sleep(.5)
        if not user.IsWindowVisible(self.window):
            raise RuntimeError("The acceptance console is not a visible native window")
        screen = memory = bitmap = previous = None
        # Raising only our own explicitly requested acceptance window prevents
        # unrelated application pixels from entering the evidence image.
        if not user.SetWindowPos(self.window, W.HWND(-1), 0, 0, 0, 0, 0x13):
            raise C.WinError(C.get_last_error())
        try:
            time.sleep(.4)
            # A user/window-manager minimize can arrive after the initial
            # visibility check; the iconic thumbnail is not visual evidence.
            if user.IsIconic(self.window):
                user.ShowWindow(self.window, 9)
                time.sleep(.5)
            rect = W.RECT()
            if not user.GetWindowRect(self.window, C.byref(rect)):
                raise C.WinError(C.get_last_error())
            dwm = C.WinDLL("dwmapi", use_last_error=True)
            dwm.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, C.c_void_p, W.DWORD]
            visible_frame = W.RECT()
            if dwm.DwmGetWindowAttribute(self.window, 9, C.byref(visible_frame), C.sizeof(visible_frame)) == 0:
                rect = visible_frame
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if not 300 <= width <= 8192 or not 200 <= height <= 8192:
                raise RuntimeError(f"Unexpected native console window dimensions: {width}x{height}")
            screen = user.GetDC(None)
            memory = gdi.CreateCompatibleDC(screen)
            bitmap = gdi.CreateCompatibleBitmap(screen, width, height)
            previous = gdi.SelectObject(memory, bitmap)
            if not gdi.BitBlt(memory, 0, 0, width, height, screen, rect.left, rect.top, 0x40CC0020):
                raise C.WinError(C.get_last_error())
            gdi.SelectObject(memory, previous)
            previous = None
            header = BITMAPINFOHEADER(C.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
            pixels = (C.c_ubyte * (width * height * 4))()
            if gdi.GetDIBits(screen, bitmap, 0, height, pixels, C.byref(header), 0) != height:
                raise C.WinError(C.get_last_error())
            bgra = bytes(pixels)
            rgba = bytearray(len(bgra))
            rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = bgra[2::4], bgra[1::4], bgra[0::4], b"\xff" * (width * height)
            rows = b"".join(b"\0" + rgba[row * width * 4:(row + 1) * width * 4] for row in range(height))
            def chunk(kind, payload):
                return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
            png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
            path = self.root / (name + ".png")
            path.write_bytes(png)
            return str(path)
        finally:
            if previous and memory:
                gdi.SelectObject(memory, previous)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory:
                gdi.DeleteDC(memory)
            if screen:
                user.ReleaseDC(None, screen)
            user.SetWindowPos(self.window, W.HWND(-2), 0, 0, 0, 0, 0x13)

    def close(self):
        self.kernel.FreeConsole()
        if self.window:
            self.user.PostMessageW(self.window, 0x0010, 0, 0)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL)


def review(root):
    """Open the production bundle with a completed fixture; do not run mutations."""
    root = Path(root).resolve(strict=True)
    if not root.is_relative_to((Path.home() / "Desktop").resolve()):
        raise ValueError("Visual review must use the Desktop acceptance fixture")
    source = Path(__file__).resolve().parents[1]
    evidence = Path(tempfile.mkdtemp(prefix="visual-review-", dir=root))
    env = dict(os.environ, GITGO_STATE_HOME=str(root / "state"), GITGO_CONFIG_PATH=str(root / "config.json"),
        GITGO_LLM_CONFIG_PATH=str(root / "llm.json"), PYTHONUTF8="1",
        GITGO_BUN=str(source / "alpha3-work/tools/bun-windows-x64/bun.exe"), GITGO_PYTHON=str(root / "runtime/python.exe"))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags, startup.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 1
    process = subprocess.Popen([str(Path(os.environ["SystemRoot"]) / "System32/conhost.exe"), sys.executable, str(root / "launch.py")],
        cwd=source, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
    console = None
    report = {"root": str(evidence), "business_result": str(root / "result.json"), "production_bundle": str(source / "cli/dashboard/dist/cli.js"),
        "production_bundle_sha256": hashlib.sha256((source / "cli/dashboard/dist/cli.js").read_bytes()).hexdigest(),
        "visual_review": "pending", "images": [], "read_only": True}
    print(json.dumps(report), flush=True)
    try:
        console = Console(process, evidence, console_client(process), visible=True)
        console.resize(120, 32)
        project = json.loads((root / "config.json").read_text(encoding="utf8"))["projects"][0]["name"]
        console.wait(project.partition("🚀")[0], "project")
        def capture(name):
            console.capture(name)
            report["images"].append(console.screenshot(name))
        capture("project-stable")
        console.send("\r")
        time.sleep(3)
        console.command("publish")
        console.send("\r")
        console.wait("本地已保存记录", "publish")
        capture("publish-stable")
        console.send("\r")
        console.send("\r")
        console.wait("最终内容树", "details")
        for width, height in ((120, 32), (80, 24), (40, 20)):
            console.resize(width, height)
            for _ in range(150):
                console.key(virtual=38)
            time.sleep(.7)
            capture(f"{width}x{height}-top")
            # Measure an actual observable scroll transition, excluding the
            # fixed driver delays used to wait for other operations.
            before, started = console.screen()[0], time.monotonic()
            console.key(virtual=40)
            deadline = started + 2
            while time.monotonic() < deadline:
                if console.screen()[0] != before:
                    report.setdefault("scroll_observation_latency_ms", []).append({"width":width,"height":height,"ms":round((time.monotonic()-started)*1000,2)})
                    break
                time.sleep(.01)
            for _ in range(23):
                console.key(virtual=40)
            time.sleep(.7)
            capture(f"{width}x{height}-scroll")
        report["capture_completed"] = True
    except BaseException as exc:
        report.update(error=type(exc).__name__, detail=str(exc))
        if console:
            capture("failure")
        raise
    finally:
        if console:
            report["initial_console_modes"] = console.initial_console_modes
            report["resize_modes"] = console.resize_modes
            console.close()
        elif process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL)
        (evidence / "visual_review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")


def diagnose(root):
    """Reopen a completed Desktop fixture for read-only renderer diagnostics."""
    root = Path(root).resolve(strict=True)
    if not root.is_relative_to((Path.home() / "Desktop").resolve()):
        raise ValueError("Diagnostics must use the Desktop acceptance fixture")
    source = Path(__file__).resolve().parents[1]
    evidence = Path(tempfile.mkdtemp(prefix="layout-diagnostic-", dir=root))
    env = dict(os.environ, GITGO_STATE_HOME=str(root / "state"), GITGO_CONFIG_PATH=str(root / "config.json"),
        GITGO_LLM_CONFIG_PATH=str(root / "llm.json"), PYTHONUTF8="1",
        GITGO_BUN=str(source / "alpha3-work/tools/bun-windows-x64/bun.exe"), GITGO_PYTHON=str(root / "runtime/python.exe"))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags, startup.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 1
    process = subprocess.Popen([str(Path(os.environ["SystemRoot"]) / "System32/conhost.exe"), sys.executable, str(root / "launch.py")],
        cwd=source, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
    console = None
    report = {"root": str(evidence), "diagnostic_only": True, "images": []}
    print(json.dumps(report), flush=True)
    try:
        console = Console(process, evidence, console_client(process), visible=True)
        console.resize(120, 32)
        project_name = json.loads((root / "config.json").read_text(encoding="utf8"))["projects"][0]["name"]
        console.wait(project_name.partition("🚀")[0], "project")
        report["inactive_width_probe"] = console.probe_widths()
        print(json.dumps(report["inactive_width_probe"], ensure_ascii=True), flush=True)
        metrics = subprocess.run([str(root / "runtime/python.exe"), "-c",
            "import json; from backend.core.application.services import ApplicationServices; print(json.dumps(ApplicationServices().config_terminals().get('display_metrics')))"],
            env=env, capture_output=True, text=True, encoding="utf8", check=True)
        report["config_terminals_display_metrics"] = json.loads(metrics.stdout)
        measured = report["config_terminals_display_metrics"]
        print(json.dumps({"config_terminals_font": measured.get("font") if measured else None,
                          "override_count": len(measured.get("codepoint_widths", {})) if measured else 0}, ensure_ascii=True), flush=True)
        probe_path, probe_result = evidence / "host-metrics.ts", evidence / "host-metrics.json"
        startup_code = """import ctypes as C, json, os
from ctypes import wintypes as W
k=C.WinDLL('kernel32',use_last_error=True); u=C.WinDLL('user32',use_last_error=True)
k.GetConsoleWindow.restype=W.HWND; k.AttachConsole.argtypes=[W.DWORD]; u.IsWindowVisible.argtypes=[W.HWND]
def state():
    window=k.GetConsoleWindow()
    pids=(W.DWORD*128)(); count=k.GetConsoleProcessList(pids,128)
    return {'window':window,'visible':bool(u.IsWindowVisible(window)) if window else False,'console_pids':list(pids)[:count],'output_cp':k.GetConsoleOutputCP()}
r={'pid':os.getpid(),'parent_pid':os.getppid(),'before':state()}
C.set_last_error(0); attached=k.AttachConsole(os.getppid()); r['attach_without_detach']={'ok':bool(attached),'error':C.get_last_error(),'state':state()}
k.FreeConsole(); C.set_last_error(0); attached=k.AttachConsole(os.getppid()); r['attach_after_detach']={'ok':bool(attached),'error':C.get_last_error(),'state':state()}
if attached:k.FreeConsole()
print(json.dumps(r))
"""
        probe_path.write_text("import {writeFileSync} from 'node:fs';\nimport {NativeHostClient} from "
            + json.dumps((source / "cli/dashboard/src/backend/client.ts").as_posix()) + ";\n"
            + "const c = new NativeHostClient(process.env.GITGO_PYTHON!, " + json.dumps(str(source)) + ", '');\n"
            + "const start = performance.now(); try { await c.start(); const query = performance.now(); "
            + "const value = await c.callTool('config.terminals', {}, 30); writeFileSync("
            + json.dumps(str(probe_result)) + ", JSON.stringify({elapsed_ms: performance.now()-query, total_ms: performance.now()-start, display_metrics: value.display_metrics})); "
            + "} finally { await c.close(); }\n"
            + "const {spawn} = await import('node:child_process'); const p = spawn(process.env.GITGO_PYTHON!, ['-c', "
            + json.dumps(startup_code) + "], {stdio:['pipe','pipe','pipe'], windowsHide:true, cwd: " + json.dumps(str(source)) + ", env:process.env}); "
            + "let out='', err=''; p.stdout.on('data', b=>out+=b); p.stderr.on('data',b=>err+=b); p.stdin.end(); "
            + "await new Promise((resolve,reject)=>{p.on('error',reject); p.on('exit',code=>code===0?resolve(null):reject(Error(err)));}); "
            + "writeFileSync(" + json.dumps(str(evidence / "host-console-startup.json")) + ",out);\n", encoding="utf8")
        before_metrics, before_screen = console.metrics(), console.screen()
        # The production Host correctly rejects a second owner of the same
        # data profile. Probe its transport using an independent local profile.
        shutil.copy2(root / "config.json", evidence / "probe-config.json")
        probe_env = dict(env, GITGO_STATE_HOME=str(evidence / "probe-state"),
            GITGO_CONFIG_PATH=str(evidence / "probe-config.json"), GITGO_LLM_CONFIG_PATH=str(evidence / "probe-llm.json"))
        host_probe = subprocess.run([env["GITGO_BUN"], str(probe_path)], env=probe_env, cwd=source, capture_output=True, timeout=45)
        (evidence / "host-metrics-stderr.txt").write_bytes(host_probe.stderr)
        if host_probe.returncode:
            raise RuntimeError("Host metrics probe failed: " + host_probe.stderr.decode("utf8", errors="replace"))
        after_metrics, after_screen = console.metrics(), console.screen()
        report["native_host_metrics"] = json.loads(probe_result.read_text(encoding="utf8"))
        report["host_console_startup"] = json.loads((evidence / "host-console-startup.json").read_text(encoding="utf8"))
        print(json.dumps(report["host_console_startup"]), flush=True)
        report["native_host_metrics"].update(active_screen_unchanged=before_screen == after_screen,
            font_unchanged=(before_metrics["font"], before_metrics["font_size"]) == (after_metrics["font"], after_metrics["font_size"]))
        measured = report["native_host_metrics"].get("display_metrics")
        print(json.dumps({"host_font": measured.get("font") if measured else None,
            "host_override_count": len(measured.get("codepoint_widths", {})) if measured else 0,
            "elapsed_ms": report["native_host_metrics"]["elapsed_ms"],
            "active_screen_unchanged": report["native_host_metrics"]["active_screen_unchanged"],
            "font_unchanged": report["native_host_metrics"]["font_unchanged"]}, ensure_ascii=True), flush=True)
        console.send("\r")
        time.sleep(3)
        console.command("publish")
        console.send("\r")
        console.wait("本地已保存记录", "publish")
        console.send("\r")
        console.send("\r")
        console.wait("最终内容树", "details")
        def capture(name):
            console.capture(name)
            report["images"].append(console.screenshot(name))
        capture("120-before-redraw")
        console.key("\x0c", 76, 8)
        time.sleep(1)
        capture("120-after-ctrl-l")
        console.resize(119, 32)
        console.resize(120, 32)
        capture("120-after-resize")
        for width, height in ((80, 24), (40, 20)):
            console.resize(width, height)
            for _ in range(150):
                console.key(virtual=38)
            time.sleep(.7)
            capture(f"{width}x{height}-top")
            for _ in range(24):
                console.key(virtual=40)
            time.sleep(.7)
            capture(f"{width}x{height}-scroll")
        report["finished"] = True
    except BaseException as exc:
        report.update(error=type(exc).__name__, detail=str(exc))
        if console:
            try:
                capture("failure")
            except Exception as capture_error:
                report["capture_error"] = str(capture_error)
        raise
    finally:
        (evidence / "result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
        if console:
            console.close()
        elif process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL)


def run(root_parent, *, visible=False):
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    desktop = Path.home() / "Desktop"
    root_parent = Path(root_parent).resolve(strict=True)
    if not root_parent.is_relative_to(desktop.resolve()):
        raise ValueError("Acceptance evidence must be placed on the Desktop")
    root = Path(tempfile.mkdtemp(prefix="terminal-m2-m3-", dir=root_parent))
    (root_parent / "terminal-m2-m3-latest.txt").write_text(str(root), encoding="utf8")
    print(json.dumps({"acceptance_root": str(root)}), flush=True)
    data = fixtures(root)
    os.environ.update(GITGO_STATE_HOME=str(root / "state"), GITGO_CONFIG_PATH=str(root / "config.json"),
                      GITGO_LLM_CONFIG_PATH=str(root / "llm.json"), PYTHONUTF8="1")
    from backend.core.config import Config, ConfigManager, ProjectConfig
    project = ProjectConfig(name="publish-组合-🚀")
    project.workspace_path = project.backup_path = str(data["workspace"])
    project.trial_path = str(data["trial"])
    project.publish_checks = [{"id": "combined", "target": "test_release.py", "seeds": [42, 7], "timeout": 30}]
    ConfigManager.save(Config(projects=[project], language="zh", theme="dark", animation=False,
        launcher={"terminal": "current", "command": "", "args": []}))
    runtime = copy_runtime(source, root)
    env = dict(os.environ, GITGO_BUN=str(source / "alpha3-work/tools/bun-windows-x64/bun.exe"), GITGO_PYTHON=str(runtime / "python.exe"))
    launcher = root / "launch.py"
    launcher.write_text("import subprocess\nsubprocess.run(" + repr('"' + str(source / "run_dashboard_native.bat") + '" --attached') + ", shell=True)\n", encoding="utf8")
    startup = subprocess.STARTUPINFO()
    startup.dwFlags, startup.wShowWindow = subprocess.STARTF_USESHOWWINDOW, 1 if visible else 0
    host = Path(os.environ["SystemRoot"]) / "System32/conhost.exe"
    process = subprocess.Popen([str(host), sys.executable, str(launcher)], cwd=source, env=env,
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
    console = None
    result = {"workflow_passed": False, "visual_review": "pending", "root": str(root), "source": str(source), "visible_terminal": visible}
    try:
        console = Console(process, root, console_client(process), visible=visible)
        console.resize(120, 32)
        console.wait(project.name.partition("🚀")[0], "project")
        console.send("\r")
        time.sleep(3)
        console.command("publish")
        console.send("\r")
        console.wait("本地已保存记录", "publish")
        console.command("combine")
        console.wait("准备本地正式整理", "combine-draft")
        for value in ("workspace", "HEAD", "trial | HEAD", "formal", "squash", "terminal-验收-🚀", "", ""):
            console.field(value)
        console.wait("已准备", "prepared")
        console.command("run verify execute")
        console.wait("已本地合入", "integrated", timeout=150)
        console.command("package")
        console.wait("Publish · package", "package-draft")
        for value in ("0.40.0-terminal", "build_release.py", "dist/release.txt", "60"):
            console.field(value)
        # Operations are observed through the saved query, never by triggering
        # backend mutations outside the terminal flow under acceptance.
        from backend.core.application.services import ApplicationServices
        services = ApplicationServices()
        def saved():
            rows = services.publish_items(project.name)["items"]
            return next((row for row in rows if row.get("identity_kind") == "stable_item"), None)
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            item = saved()
            if item and item.get("packages"):
                break
            if item and item.get("distribution_operation", {}).get("state") in {"failed", "uncertain"}:
                raise AssertionError(item["distribution_operation"])
            time.sleep(.3)
        else:
            raise RuntimeError("Native package did not finish")
        console.capture("packaged")
        time.sleep(1)
        console.command("export")
        console.wait("Publish · export", "export-draft")
        console.field(item["packages"][-1]["id"])
        console.field(str(data["export"]))
        package = item["packages"][-1]
        exported = data["export"] / package["filename"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            item = saved()
            if item.get("distribution_operation", {}).get("kind") == "export" and item["distribution_operation"]["state"] == "observed":
                break
            time.sleep(.25)
        else:
            raise RuntimeError("Export did not finish")
        console.capture("exported")
        assert item["status"] == "integrated" and item["run"]["state"] == "completed", item
        assert item["owner"] == "terminal-验收-🚀"
        assert len(item["plan"]["contributions"]) == 2
        assert {row["oid"] for row in item["plan"]["contributions"]} == {data["candidate"], data["trial_oid"]}
        assert len(item["run"]["observations"]) == 2
        assert all(any(e["kind"] == kind and e["outcome"] == "passed" and e["current"] for e in item["evidence"])
                   for kind in ("project_test", "environment", "outbound_privacy"))
        oid = git(data["workspace"], "rev-parse", "formal")
        assert git(data["workspace"], "show", "-s", "--format=%P", oid) == data["baseline"]
        assert git(data["workspace"], "rev-parse", "formal^{tree}") == item["plan"]["tree_oid"]
        assert git(data["workspace"], "rev-parse", "HEAD") == data["candidate"]
        assert git(data["workspace"], "symbolic-ref", "HEAD") == data["head_ref"]
        assert git(data["workspace"], "status", "--porcelain") == data["dirty"]
        assert (data["workspace"] / "uncommitted.txt").read_text() == "keep untouched\n"
        assert not (data["workspace"] / "dist").exists()
        checksum = hashlib.sha256(exported.read_bytes()).hexdigest()
        assert checksum == package["sha256"]
        from backend.core.publish.package import validate_package
        manifest = validate_package(exported.read_bytes(), checksum, expected_source_oid=oid, expected_tree_oid=item["plan"]["tree_oid"])
        assert manifest["artifacts"][0]["path"] == "dist/release.txt"
        assert manifest["build"]["environment"]["digest"]
        with zipfile.ZipFile(exported) as archive:
            assert archive.read("artifacts/dist/release.txt") == b"workspace contribution\ntrial contribution\n"
        result.update(workflow_passed=True, candidate_oid=data["candidate"], trial_oid=data["trial_oid"], baseline_oid=data["baseline"],
            result_oid=oid, head_ref=data["head_ref"], dirty_preserved=data["dirty"],
            exported=str(exported), sha256=checksum, manifest=manifest, item=item)
        console.send("\r")
        console.send("\r")
        console.wait("最终内容树", "details")
        layouts = []
        result["layouts"] = layouts
        for width, height in ((120, 32), (80, 24), (40, 20)):
            console.resize(width, height)
            for _ in range(150):
                console.key(virtual=38)
            time.sleep(.7)
            top = console.capture(f"layout-{width}x{height}-top")
            _, actual_width, actual_height = console.screen()
            top_image = console.screenshot(f"layout-{width}x{height}-top") if visible else None
            for _ in range(24):
                console.key(virtual=40)
            time.sleep(.7)
            scrolled = console.capture(f"layout-{width}x{height}-scroll")
            scroll_image = console.screenshot(f"layout-{width}x{height}-scroll") if visible else None
            top_header, scroll_header = "\n".join(top.splitlines()[:4]), "\n".join(scrolled.splitlines()[:4])
            layouts.append({"width": width, "height": height, "actual_width": actual_width, "actual_height": actual_height,
                            "header_visible": all("事项" in text and "设置" in text for text in (top_header, scroll_header)),
                            "footer_visible": "Esc" in top and "Esc" in scrolled,
                            "colored": len(console.phases[f"layout-{width}x{height}-top"]["color_attributes"]) > 1,
                            "top_image": top_image, "scroll_image": scroll_image})
        assert all(row["header_visible"] and row["footer_visible"] and row["colored"]
                   and (row["actual_width"], row["actual_height"]) == (row["width"], row["height"]) for row in layouts), layouts
        result.update(layout_structure_checks_passed=True)
        print(json.dumps({"workflow_passed": True, "visual_review": "pending", "root": str(root), "result_oid": oid, "sha256": checksum}), flush=True)
    except BaseException as exc:
        result.update(error=type(exc).__name__, detail=str(exc))
        if console:
            try:
                console.capture("failure")
                if visible:
                    result["failure_image"] = console.screenshot("failure")
            except Exception as capture_error:
                result["capture_error"] = str(capture_error)
        raise
    finally:
        if console:
            result["input_latencies"] = console.input_latencies
            result["initial_console_modes"] = console.initial_console_modes
            result["resize_console_modes"] = console.resize_modes
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
        if console:
            console.close()
        elif process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root")
    parser.add_argument("--diagnose-root", help="Reopen a completed Desktop fixture for read-only visual diagnostics")
    parser.add_argument("--review-root", help="Reopen the production bundle for read-only final visual evidence")
    parser.add_argument("--visible", action="store_true", help="Show the real acceptance console explicitly authorized by Issue 40")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This acceptance driver requires the real Windows console")
    if args.review_root:
        if not args.visible:
            raise SystemExit("Visual review requires --visible")
        review(args.review_root)
    elif args.diagnose_root:
        if not args.visible:
            raise SystemExit("Read-only visual diagnostics require --visible")
        diagnose(args.diagnose_root)
    elif args.root:
        run(args.root, visible=args.visible)
    else:
        parser.error("--root, --review-root or --diagnose-root is required")
