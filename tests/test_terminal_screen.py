from tests.terminal_screen import TerminalScreen


def test_incremental_repaint_reuses_cells_and_reconstructs_words():
    screen = TerminalScreen(4, 40)
    screen.feed(b"Projects\r\x1b[1GHello\x1b[1;3HXX\x1b[K")
    assert screen.text().splitlines()[0] == "HeXX"
    screen.feed(b"\r\x1b[2KGitg\x1b[1Co Software Update")
    assert screen.text().splitlines()[0] == "Gitg o Software Update"


def test_split_escape_and_utf8_sequences_do_not_become_literal_text():
    screen = TerminalScreen(3, 20)
    for chunk in (b"abc\x1b[", b"2D", "中文".encode()[:2], "中文".encode()[2:]): screen.feed(chunk)
    assert screen.text().splitlines()[0] == "a中文"


def test_screen_erase_scroll_and_cursor_position():
    screen = TerminalScreen(2, 10)
    screen.feed(b"first\r\nsecond\r\nthird")
    assert screen.text() == "second\nthird"
    assert screen.transcript() == "first\nsecond\nthird"
    screen.feed(b"\x1b[2J\x1b[Hnew")
    assert screen.text() == "new\n"


def test_style_queries_and_osc_are_not_visible_text():
    screen = TerminalScreen(2, 20)
    screen.feed(b"\x1b]0;title\x07\x1b[31mhello\x1b[0m\x1b[?25l\x1b[6n")
    assert screen.text() == "hello\n"

def test_menu_driver_uses_visible_selection_and_waits_for_new_frames():
    from tests.terminal_screen import activate_menu_item
    screen = TerminalScreen(3, 80)
    labels = ["Channel preview", "Check now", "Review restart / update"]
    index = 0
    pending = False
    calls = []
    screen.feed(("› " + labels[0]).encode())
    def send(value):
        nonlocal pending
        calls.append(value)
        if value == "\x1b[B":
            pending = True
    def pump():
        nonlocal index, pending
        if pending:
            index += 1
            screen.feed(("\x1b[2J\x1b[H› " + labels[index]).encode())
            pending = False
    activate_menu_item(screen, labels[-1], send, pump)
    assert calls == ["\x1b[B", "\x1b[B", "\r"]


def test_menu_driver_does_not_move_when_target_is_already_selected():
    from tests.terminal_screen import activate_menu_item
    screen = TerminalScreen(2, 80)
    screen.feed("› Review rollback".encode())
    calls = []
    activate_menu_item(screen, "Review rollback", calls.append,
                       lambda: (_ for _ in ()).throw(AssertionError("unexpected read")))
    assert calls == ["\r"]
