"""What every screen of rshm shares: text measured in terminal cells, fitted to a width, the one-line input, and
how it reaches a terminal that isn't UTF-8."""
import contextlib, curses, os, sys, unicodedata

MOVES = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10,
         curses.KEY_HOME: -1 << 30, curses.KEY_END: 1 << 30}
WHEEL_UP, WHEEL_DOWN = curses.BUTTON4_PRESSED, getattr(curses, "BUTTON5_PRESSED", 0x200000)
CLICK, RIGHT = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED, curses.BUTTON3_PRESSED | curses.BUTTON3_CLICKED


def utf8():  # does the terminal take UTF-8? Its locale says, read as tmux reads it: the first of LC_ALL, LC_CTYPE,
    for k in ("LC_ALL", "LC_CTYPE", "LANG"):  # LANG that's set. Python puts C.UTF-8 in LC_CTYPE over a C locale (its
        v = os.environ.get(k, "")             # UTF-8 mode on): that one is Python's, not the terminal's
        if v and not (k == "LC_CTYPE" and v == "C.UTF-8" and sys.flags.utf8_mode):
            return "UTF-8" in v.upper().replace("UTF8", "UTF-8")
    return False


ASCII = not utf8()  # decided once: a terminal that isn't UTF-8 gets one ASCII character for each one drawn
GLYPHS = str.maketrans("─│┌┐└┘├┤┬┴" "✓✗…" "→←↑↓" "·×●○▸▾▲▼" "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏‖" "█░",  # frames, marks, arrows,
                       "-|++++++++" "+!~" "><^v" "-x*o>v^v" "|/-\\|/-\\|/=" "#.")  # signs, spinner and pause, bar


def plain(s):  # s as this terminal shows it: as is on UTF-8; else ASCII, one character for each (frames +-|, ✓ +, ✗ !,
    return s.translate(GLYPHS).encode("ascii", "replace").decode() if ASCII else s  # cuts ~, spinner |/-\, ? the rest)


class Plain:  # a window on a terminal that isn't UTF-8: everything drawn goes through plain()
    def __init__(s, win):
        s.win = win

    def __getattr__(s, name):
        return getattr(s.win, name)

    def addstr(s, y, x, text, *attr):
        s.win.addstr(y, x, plain(text), *attr)

    def insstr(s, y, x, text, *attr):
        s.win.insstr(y, x, plain(text), *attr)


def window(win):  # how rshm draws on win: as is, or (not UTF-8) through Plain
    return Plain(win) if ASCII else win


def cursor(on):  # show or hide the cursor: cosmetic, so a terminal that can't (vt100) keeps it as it is
    with contextlib.suppress(curses.error):
        curses.curs_set(on)


def safe(s):  # file names and contents are arbitrary bytes: never let them drive the terminal
    return "".join(c if c.isprintable() else "?" for c in s)


def cell(c):  # the cells one character takes: 2 wide (CJK, emoji), 0 combining, else 1; not UTF-8: 1, as plain() draws it
    return 1 if ASCII else 0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1


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
            return f"{n:.0f}B" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def many(n, word):  # 1 file, 2 files
    return f"{n} {word}{'s' * (n != 1)}"


def dur(s):  # 4213 -> 1h10m, 185 -> 3m05s, 42 -> 42s
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m{s % 60:02d}s" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}m"


def bar(left, right, W, keep="end"):  # a status line W-1 cells wide: right kept (its start, on a narrow screen),
    right = span(right, 0, min(cells(right), max(W - 1, 0)))  # left fitted into what's left
    return fit(left, max(W - 1 - cells(right), 0), keep) + right


def line(scr, q, text="", attr=0, back=None, ends=(), complete=None):  # the one-line editor on the bottom row
    i, fresh, cyc, k = len(text), bool(text), None, 0  # -> (text, the key that ended it: ⏎, Esc (^C too), or one of
    cursor(1)  # ends); back() repaints behind it. ← → Home End Del ^A ^E ^K ^U ^W edit. A prefilled answer
    try:                # is selected (typing replaces it, ⌫ clears it); Tab: complete(typed) -> what it can become
        while True:
            back and back()
            H, W = scr.getmaxyx()
            col = cells(f" {q} {text[:i]}")
            start = max(0, col - W + 2)  # sideways, to keep the cursor in sight
            with contextlib.suppress(curses.error):  # a terminal too small for it: what fits shows
                scr.addstr(H - 1, 0, span(safe(f" {q} {text}"), start, max(W - 1, 0)), attr)
                if fresh and not start:
                    scr.addstr(H - 1, cells(f" {q} "), span(safe(text), 0, min(cells(text), W - 2 - cells(f" {q} "))),
                               attr | curses.A_REVERSE)
                if cyc and col + 8 < W:  # going round the matches: which one of how many
                    scr.addstr(H - 1, W - 2 - len(f"{k + 1}/{len(cyc)}"), f"{k + 1}/{len(cyc)}", attr | curses.A_REVERSE)
                scr.move(H - 1, max(0, min(col - start, W - 2)))
            c = scr.get_wch()
            if c in ("\n", "\r", curses.KEY_ENTER) or c in ends:
                return text, "\n" if c in ("\r", curses.KEY_ENTER) else c
            if c in ("\x1b", "\x03"):
                return text, "\x1b"
            was, fresh = fresh, False
            if c == "\t" and complete:  # as far as all matches agree; again (or no further): each match in turn
                if cyc:
                    k = (k + 1) % len(cyc)
                    head = cyc[k]
                else:
                    found = complete(text[:i])
                    head = os.path.commonprefix(found) if found else text[:i]
                    if len(found) > 1 and head == text[:i]:
                        cyc, k, head = found, 0, found[0]
                text, i = head + text[i:], len(head)
                continue
            cyc = None
            if c in (curses.KEY_BACKSPACE, "\x7f", "\b", curses.KEY_DC) and was:
                text, i = "", 0
            elif c in (curses.KEY_BACKSPACE, "\x7f", "\b") and i:
                text, i = text[:i - 1] + text[i:], i - 1
            elif c == curses.KEY_DC:
                text = text[:i] + text[i + 1:]
            elif c in (curses.KEY_LEFT, curses.KEY_RIGHT):
                i = max(0, min(len(text), i + (1 if c == curses.KEY_RIGHT else -1)))
            elif c in (curses.KEY_HOME, "\x01", curses.KEY_END, "\x05"):
                i = 0 if c in (curses.KEY_HOME, "\x01") else len(text)
            elif c == "\x0b":  # ^K: to the end
                text = text[:i]
            elif c == "\x15":  # ^U: to the start
                text, i = text[i:], 0
            elif c == "\x17":  # ^W: the word before
                j = text[:i].rstrip(" ").rfind(" ") + 1
                text, i = text[:j] + text[i:], j
            elif isinstance(c, str) and c.isprintable():
                text, i = (c, 1) if was else (text[:i] + c + text[i:], i + 1)
    except KeyboardInterrupt:
        return text, "\x1b"
    finally:
        cursor(0)


def readline(scr, q, text="", attr=0, back=None, complete=None):  # ⏎ takes the line as typed, Esc or ^C: None
    text, k = line(scr, q, text, attr, back, complete=complete)
    return text if k == "\n" else None
