"""rshm's own editor, with nano's keys: ^S save, ^X exit, ^W find, ^\\ replace, ^K cut and ^U paste lines, ^_ go to
line, ^Z undo. Tabs, wide characters, CRLF and bytes it can't decode come back exactly as they were."""
import curses, re

from .screen import CLICK, WHEEL_DOWN, WHEEL_UP, bar, cell, cells, cursor, key, readline, safe, span

TAB = 8


def show(ln):  # a line as it looks: tabs to their stops, measured in cells
    out, col = [], 0
    for c in ln:
        n = TAB - col % TAB if c == "\t" else cell(c)
        out.append(" " * n if c == "\t" else c)
        col += n
    return safe("".join(out))


class Buffer:  # the text being edited, with no screen: every change goes through edit(), so undo sees all of them
    def __init__(b, data):
        b.nl = "\r\n" if b"\r\n" in data else "\n"
        b.lines = data.decode("utf-8", "surrogateescape").split(b.nl)
        b.y = b.x = 0
        b.undos, b.clip, b.last, b.changed = [], [], None, False  # last: what the previous edit was (runs merge)

    def data(b):
        return b.nl.join(b.lines).encode("utf-8", "surrogateescape")

    def edit(b, y0, y1, new, y, x, run=None):  # lines[y0:y1] = new, cursor to (y, x); a run of typing is one undo
        new = new or ([""] if y1 - y0 >= len(b.lines) else [])  # a text is never less than one empty line
        if not (run and run == b.last and b.undos and b.undos[-1][:2] == (y0, y0 + 1)):
            b.undos.append((y0, y0 + len(new), b.lines[y0:y1], b.y, b.x))
            del b.undos[:-1000]
        b.lines[y0:y1] = new
        b.y, b.x, b.last, b.changed = min(y, len(b.lines) - 1), x, run, True

    def undo(b):
        if b.undos:
            y0, y1, old, b.y, b.x = b.undos.pop()
            b.lines[y0:y1] = old
            b.last, b.changed = None, True

    def insert(b, s):
        ln = b.lines[b.y]
        b.edit(b.y, b.y + 1, [ln[:b.x] + s + ln[b.x:]], b.y, b.x + len(s), "type")

    def newline(b):
        ln = b.lines[b.y]
        b.edit(b.y, b.y + 1, [ln[:b.x], ln[b.x:]], b.y + 1, 0)

    def backspace(b):
        ln = b.lines[b.y]
        if b.x:
            b.edit(b.y, b.y + 1, [ln[:b.x - 1] + ln[b.x:]], b.y, b.x - 1, "type")
        elif b.y:
            up = b.lines[b.y - 1]
            b.edit(b.y - 1, b.y + 1, [up + ln], b.y - 1, len(up))

    def delete(b):
        ln = b.lines[b.y]
        if b.x < len(ln):
            b.edit(b.y, b.y + 1, [ln[:b.x] + ln[b.x + 1:]], b.y, b.x, "type")
        elif b.y + 1 < len(b.lines):
            b.edit(b.y, b.y + 2, [ln + b.lines[b.y + 1]], b.y, b.x)

    def cut(b):  # ^K: the line into the clipboard; cuts in a row gather there
        b.clip = (b.clip if b.last == "cut" else []) + [b.lines[b.y]]
        b.edit(b.y, b.y + 1, [], b.y, 0)
        b.last = "cut"

    def paste(b):  # ^U: the clipboard's lines above the cursor's line
        if b.clip:
            b.edit(b.y, b.y, list(b.clip), b.y + len(b.clip), 0)

    def replace(b, q, r):  # every q (any case) becomes r, as one undo -> how many
        rx = re.compile(re.escape(q), re.I)
        n = sum(len(rx.findall(ln)) for ln in b.lines)
        if n:
            b.edit(0, len(b.lines), [rx.sub(lambda _: r, ln) for ln in b.lines], b.y, 0)
        return n

    def move(b, dy=0, dx=0):  # left of a line's start goes up a line, right of its end down one
        b.last = None
        if dx < 0 and b.x == 0 and b.y:
            b.y, b.x = b.y - 1, len(b.lines[b.y - 1])
        elif dx > 0 and b.x == len(b.lines[b.y]) and b.y + 1 < len(b.lines):
            b.y, b.x = b.y + 1, 0
        else:
            b.y = max(0, min(len(b.lines) - 1, b.y + dy))
            b.x = max(0, min(len(b.lines[b.y]), b.x + dx))

    def goto(b, y, col=None):  # line y (from 0); col: a screen column on it, else the line's start
        b.move()
        b.y = max(0, min(len(b.lines) - 1, y))
        ln = b.lines[b.y]
        b.x = 0 if col is None else next((x for x in range(len(ln) + 1) if cells(show(ln[:x])) >= col), len(ln))

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
    ask = lambda q: readline(scr, q, "", C["acc"] | curses.A_BOLD)
    try:  # (the terminal is raw: ^S ^Z ^C ^\\ are keys, not flow control or signals)
        while True:
            cursor(1)
            H, W = scr.getmaxyx()
            col = cells(show(b.lines[b.y][:b.x]))
            top = min(max(top, b.y - H + 2), b.y)
            left = 0 if col < W - 1 else min(max(left, col - W + 2), col)  # scrolled sideways only when it must be
            scr.erase()
            for i, ln in enumerate(b.lines[top:top + H - 1]):
                scr.addstr(i, 0, span(show(ln), left, W - 1), C["norm"])
            keys = f"{' *' if b.changed else ''}  {b.y + 1}:{b.x + 1}  ^S save ^X exit ^W find ^\\ replace ^K cut ^U paste " \
                   "^_ line ^Z undo "
            scr.addstr(H - 1, 0, bar(f" {msg or title}", keys, W), C["err" if msg[:1] == "✗" else "acc"] | curses.A_BOLD)
            scr.move(b.y - top, col - left)
            msg = ""
            c = key(scr)
            if c == "\x13":  # ^S
                try:
                    save(b.data())
                    b.changed, msg = False, "saved"
                except OSError as e:
                    msg = f"✗ not saved: {e}"
            elif c == "\x18":  # ^X
                if not b.changed:
                    return
                scr.addstr(H - 1, 0, bar(" save changes? y yes  n no  else keep editing", "", W), C["acc"] | curses.A_BOLD)
                k = key(scr)
                if k in ("n", "N"):
                    return
                if k in ("y", "Y"):
                    try:
                        return save(b.data())
                    except OSError as e:
                        msg = f"✗ not saved: {e}"
            elif c == "\x17":  # ^W
                q = ask("find:")
                if q and not b.find(q):
                    msg = f"✗ {q}: not found"
            elif c == "\x1c":  # ^\
                q = ask("replace:")
                r = q and ask(f"replace {q} with:")
                if r is not None and q:  # Esc at either question replaces nothing
                    msg = f"{b.replace(q, r)} replaced"
            elif c == "\x1f":  # ^_
                n = (ask("go to line:") or "").strip()
                if n.isdigit():
                    b.goto(int(n) - 1)
            elif c == "\x0b":  # ^K
                b.cut()
            elif c == "\x15":  # ^U
                b.paste()
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
            elif c == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bs = curses.getmouse()
                except curses.error:
                    continue
                if bs & (WHEEL_UP | WHEEL_DOWN):
                    b.move(dy=-3 if bs & WHEEL_UP else 3)
                elif bs & CLICK and my < H - 1:
                    b.goto(top + my, left + mx)
            elif isinstance(c, str) and (c.isprintable() or c == "\t"):
                b.insert(c)
    finally:
        cursor(0)
