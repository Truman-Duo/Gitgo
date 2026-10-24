"""Bounded VT display reader for English terminal acceptance fixtures.

Ink repaints by moving the cursor and reusing unchanged cells. Removing escape
codes does not reconstruct displayed text. This reader applies common movement,
erasure and scrolling commands; it is not a general terminal implementation.
"""
import codecs
import re


class TerminalScreen:
    def __init__(self, rows=40, columns=120):
        self.rows, self.columns = rows, columns
        self.lines = [[" "] * columns for _ in range(rows)]
        self.row = self.column = 0
        self.saved = (0, 0)
        self.scrollback = []
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def text(self): return "\n".join("".join(line).rstrip() for line in self.lines)

    def transcript(self):
        """Include bounded lines actually scrolled off during fullscreen writes."""
        return "\n".join(self.scrollback + [self.text()])

    def _scroll(self):
        while self.row >= self.rows:
            self.scrollback.append("".join(self.lines.pop(0)).rstrip())
            self.scrollback = self.scrollback[-80:]
            self.lines.append([" "] * self.columns); self.row -= 1

    def _csi(self, params, command):
        private = params.startswith("?")
        values = [int(v) if v.isdigit() else 0 for v in params.lstrip("?").split(";")]
        n = values[0] or 1
        if private:
            if command == "h" and 1049 in values:
                self.lines = [[" "] * self.columns for _ in range(self.rows)]
                self.row = self.column = 0
            return
        if command == "A": self.row = max(0, self.row - n)
        elif command == "B": self.row = min(self.rows - 1, self.row + n)
        elif command == "C": self.column = min(self.columns - 1, self.column + n)
        elif command == "D": self.column = max(0, self.column - n)
        elif command == "E": self.row = min(self.rows - 1, self.row + n); self.column = 0
        elif command == "F": self.row = max(0, self.row - n); self.column = 0
        elif command == "G": self.column = min(self.columns - 1, n - 1)
        elif command in ("H", "f"):
            self.row = min(self.rows - 1, (values[0] or 1) - 1)
            self.column = min(self.columns - 1, ((values[1] if len(values) > 1 else 1) or 1) - 1)
        elif command == "K":
            start, end = ((0, self.columns) if values[0] == 2 else
                          (0, self.column + 1) if values[0] == 1 else (self.column, self.columns))
            self.lines[self.row][start:end] = [" "] * (end - start)
        elif command == "J":
            if values[0] in (2, 3): self.lines = [[" "] * self.columns for _ in range(self.rows)]
            elif values[0] == 1:
                for row in range(self.row): self.lines[row] = [" "] * self.columns
                self.lines[self.row][:self.column + 1] = [" "] * (self.column + 1)
            else:
                self.lines[self.row][self.column:] = [" "] * (self.columns - self.column)
                for row in range(self.row + 1, self.rows): self.lines[row] = [" "] * self.columns
        elif command == "s": self.saved = (self.row, self.column)
        elif command == "u": self.row, self.column = self.saved
        elif command in ("S", "T"):
            for _ in range(min(n, self.rows)):
                if command == "S": self.lines.pop(0); self.lines.append([" "] * self.columns)
                else: self.lines.pop(); self.lines.insert(0, [" "] * self.columns)
        elif command == "X":
            end = min(self.columns, self.column + n)
            self.lines[self.row][self.column:end] = [" "] * (end - self.column)
        # SGR, cursor visibility and terminal queries do not change text cells.

    def feed(self, chunk):
        self.buffer += self.decoder.decode(chunk)
        while self.buffer:
            if self.buffer.startswith("\x1b["):
                match = re.match(r"\x1b\[([0-?]*)([ -/]*)([@-~])", self.buffer)
                if not match: break
                self._csi(match[1], match[3]); self.buffer = self.buffer[match.end():]; continue
            if self.buffer.startswith("\x1b]"):
                match = re.search(r"\x07|\x1b\\", self.buffer[2:])
                if not match: break
                self.buffer = self.buffer[2 + match.end():]; continue
            if self.buffer[0] == "\x1b":
                if len(self.buffer) < 2: break
                if self.buffer[1] == "7": self.saved = (self.row, self.column)
                elif self.buffer[1] == "8": self.row, self.column = self.saved
                self.buffer = self.buffer[2:]; continue
            char, self.buffer = self.buffer[0], self.buffer[1:]
            if char == "\r": self.column = 0
            elif char == "\n": self.row += 1; self._scroll()
            elif char == "\b": self.column = max(0, self.column - 1)
            elif char == "\t": self.column = min(self.columns - 1, (self.column // 8 + 1) * 8)
            elif ord(char) >= 32:
                if self.column >= self.columns: self.column = 0; self.row += 1; self._scroll()
                self.lines[self.row][self.column] = char; self.column += 1
        if len(self.buffer) > 1024: raise ValueError("Unbounded terminal control sequence")

def activate_menu_item(screen, label, send, pump, *, maximum_rows=12, timeout=15):
    """Use the rendered enabled selection, never obsolete action hotkeys.

    The caller owns the real terminal and cleanup. pump() reads a fresh VT
    frame; this helper cannot invoke backend actions or select disabled rows.
    """
    import time
    deadline = time.monotonic() + timeout

    def selected():
        return next((line.lstrip()[2:].strip() for line in screen.text().splitlines()
                     if line.lstrip().startswith("› ")), None)

    def advance_until(predicate):
        while not predicate():
            assert time.monotonic() < deadline, f"Menu selection did not reach {label!r}"
            pump()

    advance_until(lambda: selected() is not None)
    for _ in range(maximum_rows):
        current = selected()
        if current == label:
            send("\r")
            return
        send("\x1b[B")
        advance_until(lambda: selected() is not None and selected() != current)
    raise AssertionError(f"Enabled menu item is unavailable: {label!r}")
