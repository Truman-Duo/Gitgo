# External prompt editor return and terminal history

Ctrl+G pauses the formal Ink renderer and suspends its input while the external
editor is open. On return, Gitgo now requests an atomic full redraw before
resuming input. The previous order resumed rendering and then reset the frame
buffers, leaving cached node blits inconsistent with the physical display.

`Ink.repaint()` now preserves the viewport-sized frames while the alternate
screen is active. A previous frame with height zero was treated as growing
output; the final CR+LF scrolled the physical terminal by a row without moving
the cached frame. This explains the extra row, duplicated input boundary and
persistent displacement until resize. All frame resets also invalidate the
node blit source. Both PowerShell-hosted and Git Bash-hosted dashboards use
this renderer and editor adapter; Windows defaults to the alternate screen.

Regression tests exercise the production renderer and CommandBar in main and
alternate screen modes, covering short CJK input, multiline CRLF imports, long
wrapped drafts, further editing and clearing the input without a resize. They
check cached conversation content, one NORMAL boundary, an in-viewport caret,
and absence of the growing-frame LF output in alternate-screen repaint.
These code/stream tests do not constitute visible terminal acceptance.

To inspect the formal UI, open `test_terminal_selection.bat`, choose Git Bash
(MinTTY), save, enter a project, press Ctrl+G, save a short draft in the editor,
and close the editor. Check that the text stays inside one input boundary,
editing continues at the caret, and sending leaves the full layout aligned
without resizing. Repeat with multiline Chinese and a draft wider than the
input. For PowerShell, invoke the same BAT from an existing PowerShell terminal
and choose Current terminal; repeat the same checks. The formal frontend,
layout, scene bars and native Host stay in use in both procedures.

The terminal-test BAT deliberately creates an empty, isolated state database;
it copies configuration and provider files, not normal conversation history.
Test conversations are persisted in that temporary database and CAS, then
removed on test exit. Normal launches share the user state home regardless of
terminal (`%USERPROFILE%\.gitgo\state` by default on Windows). The test startup notice
now explicitly mentions isolated history and conversation removal.

Read-only inspection confirmed that the normal and isolated test databases
held different histories. Raw message rows include tools and internal steering,
so their counts cannot be interpreted as visible chat-bubble counts. User
conversation text, project identifiers and machine-specific paths are omitted
from this public report.

A SQLite backup plus the test project's CAS was preserved at
`.gitgo/diagnostics/ctrl-g-20261008/` before cleanup. It survives the temporary
profile's exit, contains no provider configuration or secret-store copy, and
was not merged into the live user database. The source database was opened in
SQLite read-only mode; the original database remains unchanged.
