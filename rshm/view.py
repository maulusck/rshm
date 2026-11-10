"""rshm's own viewer, with less's keys: space/b pages, j/k lines, g/G ends (N g: line N), / ? search, n N again,
w wrap, x hex, q back. Binary opens as hex. The wheel scrolls."""
import curses

from .screen import MOVES, WHEEL_DOWN, WHEEL_UP, bar, cursor, readline, safe, span, wrap


def hexrow(data, i):  # row i of a hex dump: offset, 16 bytes, their printable side
    b = data[i * 16:i * 16 + 16]
    return f"{i * 16:08x}  {b[:8].hex(' '):<23}  {b[8:].hex(' '):<23}  |{''.join(chr(c) if 32 <= c < 127 else '.' for c in b)}|"


def rows(lines, w, wrapped):  # screen rows as (line number, text): wrapped onto more rows, or one row per line
    return [(i, r) for i, ln in enumerate(lines) for r in (wrap(ln, w) if wrapped else [ln])]


def find(n, row, q, start, step):  # the next of n rows (from start, step ±1, wrapping) holding q, any case; None if none
    q = q.lower()
    return next((i % n for i in range(start, start + step * n, step) if q in row(i % n).lower()), None)


def text(data):  # bytes as lines to show: CRLF or LF, tabs to 8, bytes that aren't UTF-8 as �, nothing raw
    lines = data.decode("utf-8", "replace").expandtabs(8).split("\n")
    lines = lines[:-1] if lines[-1] == "" and len(lines) > 1 else lines  # a final newline ends a line, adds none
    return [safe(ln.removesuffix("\r")) for ln in lines]


def view(scr, data, title, C):  # C: the theme's colours by role
    hexed, wrapped, top, q, msg, hit, count = b"\0" in data[:8192], True, 0, "", "", None, ""  # hit: the last match
    lines = text(data)
    cursor(0)
    while True:
        H, W = scr.getmaxyx()
        if hexed:
            n, row, line = (len(data) + 15) // 16 or 1, lambda i: hexrow(data, i), lambda i: i
        else:
            rs = rows(lines, W - 1, wrapped)
            n, row, line = len(rs), lambda i: rs[i][1], lambda i: rs[i][0]
        top = max(0, min(top, n - max(H - 1, 1)))  # a row shows, even on a 1-row screen
        scr.erase()
        for y in range(min(H - 1, n - top)):
            scr.addstr(y, 0, span(row(top + y), 0, W - 1), C["sel" if top + y == hit else "norm"])
        where = f"{count}g" if count else f"{'byte ' + str(top * 16) if hexed else 'line ' + str(line(top) + 1)}  " \
                                         f"{min(top + H - 1, n) * 100 // n}%"
        keys = f"  {where}  / ? find  n N  w wrap  x {'text' if hexed else 'hex'}  q back "
        scr.addstr(H - 1, 0, bar(f" {msg or title}", keys, W), C["err" if msg else "acc"] | curses.A_BOLD)
        msg = ""
        c = scr.getch()
        if ord("0") <= c <= ord("9"):
            count += chr(c)
            continue
        n_, count = int(count) if count else None, ""
        if c in (ord("q"), 27, 3, curses.KEY_F3, curses.KEY_F10):
            return
        if c in (ord("g"), ord("G")) and n_ is not None:  # N g: line N (byte row N in hex)
            top = next((i for i in range(n) if line(i) >= n_ - 1), n)
        elif c in MOVES or c in (ord("g"), ord("G")):
            d = MOVES.get(c, -1 << 30 if c == ord("g") else 1 << 30)
            top += d if abs(d) != 10 else d // 10 * (H - 2)
        elif c in (ord(" "), ord("f"), ord("b")):
            top += (H - 2) * (-1 if c == ord("b") else 1)
        elif c == curses.KEY_MOUSE:
            try:
                b = curses.getmouse()[4]
            except curses.error:
                continue
            top += -3 if b & WHEEL_UP else 3 if b & WHEEL_DOWN else 0
        elif c == ord("w"):
            wrapped, hit = not wrapped, None
        elif c == ord("x"):
            hexed, top, hit = not hexed, 0, None
        elif c in (ord("/"), ord("?")) or c in (ord("n"), ord("N")) and q:
            back = c in (ord("?"), ord("N"))
            if c in (ord("/"), ord("?")):
                q = readline(scr, "/" if c == ord("/") else "?", "", C["acc"] | curses.A_BOLD)
                if not q:
                    continue
            step = -1 if back else 1
            if hexed:  # bytes, not rows: a match may straddle two
                b = q.encode(errors="surrogateescape")
                at = data.rfind(b, 0, top * 16) if back else data.find(b, (top + 1) * 16)
                i = at // 16 if at >= 0 else None
            else:
                i = find(n, row, q, top + step, step)
            if i is None:
                msg = f"✗ {q}: not found"
            else:
                top = hit = i

