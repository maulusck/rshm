"""rshm's own viewer: text, or hex for binary (x switches); / searches, n/N go on, w wraps long lines."""
import curses

from .screen import MOVES, bar, readline, safe


def hexrow(data, i):  # row i of a hex dump: offset, 16 bytes, their printable side
    b = data[i * 16:i * 16 + 16]
    return f"{i * 16:08x}  {b[:8].hex(' '):<23}  {b[8:].hex(' '):<23}  |{''.join(chr(c) if 32 <= c < 127 else '.' for c in b)}|"


def rows(lines, w, wrap):  # text lines as screen rows: cut at the edge, or wrapped onto more rows
    if not wrap:
        return lines
    return [ln[i:i + w] for ln in lines for i in range(0, max(len(ln), 1), w)]


def find(n, row, q, start, step):  # the next of n rows (from start, step ±1, wrapping) holding q, any case; None if none
    q = q.lower()
    return next((i % n for i in range(start, start + step * n, step) if q in row(i % n).lower()), None)


def view(scr, data, title, C):  # C: the theme's colors (norm, sel, acc, dim, err, brand)
    hexed, wrap, top, q, msg = b"\0" in data[:8192], True, 0, "", ""
    lines = [safe(ln) for ln in data.decode("utf-8", "replace").expandtabs(8).split("\n")]
    if lines[-1:] == [""] and len(lines) > 1:
        lines.pop()  # the final newline ends the last line; it isn't one more
    curses.curs_set(0)
    while True:
        H, W = scr.getmaxyx()
        if hexed:
            n, row = (len(data) + 15) // 16 or 1, lambda i: hexrow(data, i)
        else:
            rs = rows(lines, W - 1, wrap)
            n, row = len(rs), rs.__getitem__
        top = max(0, min(top, n - (H - 1)))
        scr.erase()
        for y in range(min(H - 1, n - top)):
            scr.addnstr(y, 0, row(top + y), W - 1, C[0])
        where = f"{min(top + H - 1, n) * 100 // n}%"
        keys = f"  {where}  / find  n N  w wrap{'' if wrap else ' off'}  x {'text' if hexed else 'hex'}  q quit "
        scr.addnstr(H - 1, 0, bar(f" {msg or safe(title)}", keys, W), W - 1, C[4 if msg else 2] | curses.A_BOLD)
        msg = ""
        c = scr.getch()
        if c in (ord("q"), 27, curses.KEY_F3, curses.KEY_F10):
            return
        if c in MOVES:
            top += MOVES[c] if abs(MOVES[c]) != 10 else MOVES[c] // 10 * (H - 2)
        elif c == ord(" "):
            top += H - 2
        elif c == ord("w"):
            wrap = not wrap
        elif c == ord("x"):
            hexed = not hexed
            top = 0
        elif c == ord("/") or c in (ord("n"), ord("N")) and q:
            if c == ord("/"):
                q = readline(scr, H - 1, "find:", "", C[2] | curses.A_BOLD)
                if not q:
                    continue
            step = -1 if c == ord("N") else 1
            if hexed:  # bytes, not rows: a match may straddle two
                b = q.encode(errors="surrogateescape")
                at = data.find(b, (top + 1) * 16) if step > 0 else data.rfind(b, 0, top * 16)
                i = at // 16 if at >= 0 else None
            else:
                i = find(n, row, q, top + step, step)
            if i is None:
                msg = f"✗ {safe(q)}: not found"
            else:
                top = i
