"""What every screen of rshm shares: safe text, sizes, cursor keys, and the one-line input."""
import curses

MOVES = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10,
         curses.KEY_HOME: -1 << 30, curses.KEY_END: 1 << 30}


def safe(s):  # file names and contents are arbitrary bytes: never let them drive the terminal
    return "".join(c if c.isprintable() else "?" for c in s)


def human(n):  # 1536 -> 1.5K
    if n is None:
        return ""
    for u in "BKMGT":
        if n < 1024 or u == "T":
            return f"{n}B" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def bar(left, right, W):  # a status line: right kept whole, left cut from its start when they don't both fit
    room = W - 1 - len(right)
    left = left if len(left) <= room else "…" + left[len(left) - room + 1:] if room > 1 else ""
    return left.ljust(room) + right


def readline(scr, y, q, text="", attr=0):  # ⏎ takes the line, Esc or ^C cancels (""), ^U clears it
    curses.curs_set(1)
    try:
        while True:
            W = scr.getmaxyx()[1]
            line = f" {q} {text}"
            shown = line if len(line) < W - 1 else "…" + line[len(line) - W + 3:]  # keep the end in sight
            scr.addnstr(y, 0, shown.ljust(W - 1), W - 1, attr)
            scr.move(y, min(len(shown), W - 2))
            c = scr.get_wch()
            if c in ("\n", "\r", curses.KEY_ENTER):
                return text.strip()
            if c in ("\x1b", "\x03"):
                return ""
            if c in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                text = text[:-1]
            elif c == "\x15":
                text = ""
            elif isinstance(c, str) and c.isprintable():
                text += c
    except KeyboardInterrupt:
        return ""
    finally:
        curses.curs_set(0)
