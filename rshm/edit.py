"""rshm's own editor, nano-sized: type, ^S save, ^Q quit, ^F find, ^Z undo. Bytes it can't decode, tabs and CRLF
line ends come back exactly as they were."""
import curses

from .screen import bar, readline, safe


class Buffer:  # the text being edited, with no screen: every change goes through edit(), so undo sees all of them
    def __init__(b, data):
        b.nl = "\r\n" if b"\r\n" in data else "\n"
        b.lines = data.decode("utf-8", "surrogateescape").split(b.nl)
        b.y = b.x = 0
        b.undos, b.typing, b.changed = [], False, False

    def data(b):
        return b.nl.join(b.lines).encode("utf-8", "surrogateescape")

    def edit(b, y0, y1, new, y, x, typing=False):  # lines[y0:y1] = new, cursor to (y, x)
        if not (typing and b.typing and b.undos and b.undos[-1][:2] == (y0, y0 + 1)):  # a run of typing: one undo
            b.undos.append((y0, y0 + len(new), b.lines[y0:y1], b.y, b.x))
            del b.undos[:-1000]
        b.lines[y0:y1] = new
        b.y, b.x, b.typing, b.changed = y, x, typing, True

    def undo(b):
        if b.undos:
            y0, y1, old, b.y, b.x = b.undos.pop()
            b.lines[y0:y1] = old
            b.typing, b.changed = False, True

    def insert(b, s):
        ln = b.lines[b.y]
        b.edit(b.y, b.y + 1, [ln[:b.x] + s + ln[b.x:]], b.y, b.x + len(s), typing=True)

    def newline(b):
        ln = b.lines[b.y]
        b.edit(b.y, b.y + 1, [ln[:b.x], ln[b.x:]], b.y + 1, 0)

    def backspace(b):
        ln = b.lines[b.y]
        if b.x:
            b.edit(b.y, b.y + 1, [ln[:b.x - 1] + ln[b.x:]], b.y, b.x - 1, typing=True)
        elif b.y:
            up = b.lines[b.y - 1]
            b.edit(b.y - 1, b.y + 1, [up + ln], b.y - 1, len(up))

    def delete(b):
        ln = b.lines[b.y]
        if b.x < len(ln):
            b.edit(b.y, b.y + 1, [ln[:b.x] + ln[b.x + 1:]], b.y, b.x, typing=True)
        elif b.y + 1 < len(b.lines):
            b.edit(b.y, b.y + 2, [ln + b.lines[b.y + 1]], b.y, b.x)

    def move(b, dy=0, dx=0):  # left of a line's start goes up a line, right of its end down one
        b.typing = False
        if dx < 0 and b.x == 0 and b.y:
            b.y, b.x = b.y - 1, len(b.lines[b.y - 1])
        elif dx > 0 and b.x == len(b.lines[b.y]) and b.y + 1 < len(b.lines):
            b.y, b.x = b.y + 1, 0
        else:
            b.y = max(0, min(len(b.lines) - 1, b.y + dy))
            b.x = max(0, min(len(b.lines[b.y]), b.x + dx))

    def find(b, q):  # the next match after the cursor, any case, wrapping round; False if none
        q = q.lower()
        for i in range(len(b.lines) + 1):
            y = (b.y + i) % len(b.lines)
            x = b.lines[y].lower().find(q, b.x + 1 if i == 0 else 0)
            if x >= 0:
                b.move()
                b.y, b.x = y, x
                return True
        return False


def edit(scr, data, title, C, save):  # save(bytes) stores it; an OSError from it is shown and editing goes on
    b, top, left, msg = Buffer(data), 0, 0, ""
    curses.raw()  # ^S ^Q ^Z ^C are keys here, not terminal flow control or signals
    curses.curs_set(1)
    try:
        while True:
            H, W = scr.getmaxyx()
            top = min(max(top, b.y - H + 2), b.y)
            left = min(max(left, b.x - W + 2), b.x)
            scr.erase()
            for i, ln in enumerate(b.lines[top:top + H - 1]):  # ponytail: a tab shows as one », wide chars as one cell
                scr.addnstr(i, 0, safe(ln[left:left + W - 1].replace("\t", "»")), W - 1, C[0])
            keys = f"{' *' if b.changed else ''}  {b.y + 1}:{b.x + 1}  ^S save  ^Q quit  ^F find  ^Z undo "
            scr.addnstr(H - 1, 0, bar(f" {msg or safe(title)}", keys, W), W - 1, C[4 if msg[:1] == "✗" else 2] | curses.A_BOLD)
            scr.move(b.y - top, b.x - left)
            msg = ""
            c = scr.get_wch()
            if c == "\x13":  # ^S
                try:
                    save(b.data())
                    b.changed, msg = False, "saved"
                except OSError as e:
                    msg = f"✗ not saved: {e}"
            elif c == "\x11":  # ^Q (not Esc: a key the terminal half-sends would read as one)
                if not b.changed:
                    return
                scr.addnstr(H - 1, 0, " save changes? y yes  n no  else keep editing".ljust(W - 1), W - 1, C[2] | curses.A_BOLD)
                k = scr.get_wch()
                if k == "n":
                    return
                if k == "y":
                    try:
                        return save(b.data())
                    except OSError as e:
                        msg = f"✗ not saved: {e}"
            elif c == "\x06":  # ^F
                q = readline(scr, H - 1, "find:", "", C[2] | curses.A_BOLD)
                curses.curs_set(1)
                if q and not b.find(q):
                    msg = f"✗ {safe(q)}: not found"
            elif c == "\x1a":  # ^Z
                b.undo()
            elif c in ("\n", "\r", curses.KEY_ENTER):
                b.newline()
            elif c in ("\x7f", "\b", curses.KEY_BACKSPACE):
                b.backspace()
            elif c == curses.KEY_DC:
                b.delete()
            elif c in (curses.KEY_HOME, "\x01"):
                b.move(dx=-b.x)
            elif c in (curses.KEY_END, "\x05"):
                b.move(dx=len(b.lines[b.y]) - b.x)
            elif c in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
                b.move(dy={curses.KEY_UP: -1, curses.KEY_DOWN: 1, curses.KEY_PPAGE: 2 - H, curses.KEY_NPAGE: H - 2}[c])
            elif c in (curses.KEY_LEFT, curses.KEY_RIGHT):
                b.move(dx=-1 if c == curses.KEY_LEFT else 1)
            elif isinstance(c, str) and (c.isprintable() or c == "\t"):
                b.insert(c)
    finally:
        curses.noraw()
        curses.cbreak()
        curses.curs_set(0)
