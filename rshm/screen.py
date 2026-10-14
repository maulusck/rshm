"""What every screen of rshm shares: text measured in terminal cells, fitted to a width, the one-line input."""
import contextlib, curses, unicodedata

MOVES = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10,
         curses.KEY_HOME: -1 << 30, curses.KEY_END: 1 << 30}
WHEEL_UP, WHEEL_DOWN = curses.BUTTON4_PRESSED, getattr(curses, "BUTTON5_PRESSED", 0x200000)
CLICK, RIGHT = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED, curses.BUTTON3_PRESSED | curses.BUTTON3_CLICKED


def safe(s):  # file names and contents are arbitrary bytes: never let them drive the terminal
    return "".join(c if c.isprintable() else "?" for c in s)


def cell(c):  # the cells one character takes: 2 wide (CJK, emoji), 0 combining, else 1
    return 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1


def cells(s):
    return sum(map(cell, s))


def span(s, start, w):  # the part of s that shows from column start, w columns wide, padded: never a half character
    out, col = [], 0
    for c in s:
        n = cell(c)
        if col >= start and col + n <= start + w:
            out.append(c)
        col += n
        if col >= start + w:
            break
    t = "".join(out)
    return t + " " * (w - cells(t))


def fit(s, w, keep="end"):  # s in exactly w cells; too long, cut with … where it hurts least:
    s = safe(s)                 # keep="end" (paths, bars) cuts the start, "mid" (names) the middle
    if cells(s) > w:
        tail = lambda n: span(s[::-1], 0, n).rstrip()[::-1]
        if w < 2:
            s = span(s, 0, w)
        elif keep == "mid":
            head = span(s, 0, (w - 1) // 2).rstrip()
            s = head + "…" + tail(w - 1 - cells(head))
        else:
            s = "…" + tail(w - 1)
    return s + " " * (w - cells(s))


def wrap(s, w):  # s cut into rows of w cells (at least one character each)
    w = max(w, 1)
    if s.isascii():  # the common case, fast
        return [s[i:i + w] for i in range(0, max(len(s), 1), w)]
    rows, row, n = [], "", 0
    for c in s:
        if n + cell(c) > w:
            rows.append(row)
            row, n = "", 0
        row, n = row + c, n + cell(c)
    return [*rows, row]


def human(n):  # 1536 -> 1.5K
    if n is None:
        return ""
    for u in "BKMGT":
        if n < 1024 or u == "T":
            return f"{n}B" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def bar(left, right, W, keep="end"):  # a status line W-1 cells wide: right kept (its start, on a narrow screen),
    right = span(right, 0, min(cells(right), max(W - 1, 0)))  # left fitted into what's left
    return fit(left, max(W - 1 - cells(right), 0), keep) + right


def readline(scr, q, text="", attr=0, back=None):  # on the bottom row: ⏎ takes the line as typed, Esc or ^C
    curses.curs_set(1)                                # cancels (None), ^U clears it; back() repaints what's behind
    try:
        while True:
            back and back()
            H, W = scr.getmaxyx()
            with contextlib.suppress(curses.error):  # a terminal too small for it: what fits shows
                scr.addstr(H - 1, 0, fit(f" {q} {text}", W - 1), attr)
                scr.move(H - 1, max(0, min(cells(f" {q} {text}"), W - 2)))
            c = scr.get_wch()
            if c in ("\n", "\r", curses.KEY_ENTER):
                return text
            if c in ("\x1b", "\x03"):
                return None
            if c in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                text = text[:-1]
            elif c == "\x15":
                text = ""
            elif isinstance(c, str) and c.isprintable():
                text += c
    except KeyboardInterrupt:
        return None
    finally:
        curses.curs_set(0)
