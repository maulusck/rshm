"""The screen, mc's way: a menu bar, two panes (each a folder here or on a host, or the host list), the activity log,
the F-key bar. Every action is one row of KEYS: keys, menu, F-key bar, F1 and --help all come from there."""
import atexit, contextlib, curses, fnmatch, grp, os, posixpath, pwd, shlex, shutil, signal, socket, stat, subprocess, sys
import tempfile, time, tomllib
from collections import deque

from . import config, edit as editor, view as viewer
from .fs import (ARCHIVES, PART, Archive, Local, Remote, copy, folder, isdir, islink, join, kind, look, marker, names, peek,
                 rm, save, special, walk)
from .screen import CLICK, MOVES, RIGHT, WHEEL_DOWN, WHEEL_UP, bar, cells, fit, human, line, readline, safe
from .ssh import argv, connect, quiet, unreachable

ROLES = ("norm", "sel", "acc", "dim", "err", "bar", "dir", "exe", "link", "stale", "archive", "media", "special",
         "hidden", "file", "ok")
THEMES = {  # (fg, bg[, b(old) d(im)]) per role; -1 = the terminal's own
    "default": [(-1, -1), (0, 6), (6, -1, "b"), (-1, -1, "d"), (1, -1, "b"), (0, 6), (4, -1, "b"), (2, -1, "b"), (6, -1),
                (1, -1), (5, -1), (3, -1), (3, -1, "b"), (-1, -1, "d"), (-1, -1), (2, -1, "b")],  # your terminal's colours
    "matrix": [(34, 16), (16, 46), (46, 16, "b"), (22, 16), (196, 16, "b"), (46, 22), (46, 16, "b"), (120, 16, "b"),
               (37, 16), (124, 16), (112, 16), (77, 16), (226, 16), (28, 16), (34, 16), (46, 16, "b")],  # phosphor
    "amber": [(214, 16), (16, 214), (220, 16, "b"), (130, 16), (196, 16, "b"), (222, 94), (220, 16, "b"), (228, 16, "b"),
              (179, 16), (124, 16), (215, 16), (222, 16), (231, 16, "b"), (94, 16), (214, 16), (228, 16, "b")],  # amber CRT
    "frutiger": [(24, 195), (231, 38), (31, 195, "b"), (109, 195), (160, 195, "b"), (231, 33), (25, 195, "b"),
                 (28, 195, "b"), (37, 195), (160, 195), (97, 195), (35, 195), (130, 195, "b"), (109, 195),
                 (24, 195), (28, 195, "b")],  # Aero: sky, aqua, glass
    "teletext": [(231, 16), (16, 226), (51, 16, "b"), (27, 16), (196, 16, "b"), (226, 21), (226, 16, "b"), (46, 16, "b"),
                 (51, 16), (196, 16), (201, 16), (46, 16), (201, 16, "b"), (27, 16), (231, 16), (46, 16, "b")],  # Ceefax
}
MONO = dict(zip(ROLES, (0, curses.A_REVERSE, curses.A_BOLD, curses.A_DIM, curses.A_BOLD | curses.A_STANDOUT,
                        curses.A_REVERSE, curses.A_BOLD, curses.A_BOLD, 0, curses.A_DIM, 0, 0, curses.A_BOLD,
                        curses.A_DIM, 0, curses.A_BOLD)))
STYLE = {"b": curses.A_BOLD, "d": curses.A_DIM}
NAMES = {"⏎": (10, 13, curses.KEY_ENTER), "→": (curses.KEY_RIGHT,), "←": (curses.KEY_LEFT,), "Space": (32,),
         "⌫": (curses.KEY_BACKSPACE, 127, 8), "Del": (curses.KEY_DC,), "Ins": (curses.KEY_IC,), "Tab": (9,),
         "Esc": (27,), **{f"^{c}": (ord(c) - 64,) for c in "FLORU"}, **{f"F{i}": (curses.KEY_F0 + i,) for i in range(1, 11)}}
MENUS = ("Pane 1", "File", "Mark", "Command", "Options", "Pane 2")
KEYS = [  # (menu, keys, what, method): the one table; "Pane" rows show under both pane menus; no menu: help only
    ("File", "⏎ → l", "open", "open"), ("File", "← h ⌫", "parent folder", "parent"),
    ("File", "i", "info: everything about it", "info"), ("File", "F3 v", "view", "view"), ("File", "F4 e", "edit", "edit"),
    ("File", "F5 c", "copy to the other pane", "copy"), ("File", "F6 m", "move to the other pane", "move"),
    ("File", "F7 n", "mkdir: new folder", "mkdir"), ("File", "F8 x Del", "delete", "delete"),
    ("File", "R", "rename", "rename"), ("File", "A", "attributes: mode, owner, time", "chattr"),
    ("File", "L", "symlink to it (on a link: retarget)", "symlink"),
    ("File", "H", "hard link to it", "hardlink"),
    ("File", "z", "size", "size"),
    ("Mark", "Space Ins", "mark", "mark"), ("Mark", "*", "all / none", "markall"),
    ("Mark", "+", "mark names like…", "markpat"), ("Mark", "-", "unmark names like…", "unmarkpat"),
    ("Mark", "D", "differences from the other pane", "compare"),
    ("Pane", "@", "host list", "hostlist"), ("Pane", "Tab", "other pane", "tab"), ("Pane", "/", "filter", "search"),
    ("Pane", "S", "sort by…", "sort"), ("Pane", ".", "dotfiles: show / hide", "dotfiles"),
    ("Pane", "b", "back", "back"), ("Pane", "f", "forward", "forward"), ("Pane", "g", "go to path", "goto"),
    ("Pane", "~", "home", "home"), ("Pane", "^F", "find names like…", "find"), ("Pane", "o", "other pane here", "mirror"),
    ("Pane", "^U", "swap panes", "swap"), ("Pane", "r ^R", "reload", "reload"),
    ("Pane", "Esc", "back: filter, marks, find, host list", "esc"),
    ("Command", "F2", "menu: what can be done here", "menu"), ("Command", "^O s", "shell here", "shell"),
    ("Command", "p", "live check of the host", "ping"),
    ("Options", "F9", "bar: the menus up top", "menubar"), ("Options", "t", "theme", "theme"),
    ("Options", "[ ]", "log: older / newer", "logscroll"), ("Options", "", "edit config", "editcfg"),
    ("Options", "^L", "draw the screen again", "draw"), ("Options", "F1 ?", "help: all keys", "help"),
    ("Options", "F10 q", "quit", "quit"),
    (None, "↑ ↓ j k PgUp PgDn Home End", "move", None), (None, "click · again", "select · open", None),
    (None, "right-click", "menu of what's under it (F2)", None), (None, "wheel", "scroll (panes, log)", None),
]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
HEAD = 1 << 20  # rshm's viewer shows a file's first HEAD bytes; its editor takes files up to HEAD
ORDER = {"name": lambda x: x[0].casefold(), "extension": lambda x: (posixpath.splitext(x[0])[1].casefold(), x[0]),
         "size": lambda x: -x[1].get("size", 0), "date": lambda x: -x[1].get("mtime", 0)}
LOCAL = {"host": "local"}  # the host-list entry for this machine
ME = f"{names(os.getuid(), os.getgid())[0]}@{socket.gethostname().split('.')[0]}"  # where you are, on the menu bar
SHELL = 'exec "${SHELL:-sh}" -l'
MIN_W, MIN_H, PANE_W, PANE_H = 20, 7, 24, 4  # the whole screen, and one pane (title row included), at the least


def keys_help():  # `rshm --help` shows the same table the app runs on
    return "\n".join(f"  {k:<26} {w}" for _, k, w, _ in KEYS)


def codes(keys):  # "x Del" -> every keycode it stands for
    return [c for k in keys.split() for c in (NAMES[k] if k in NAMES else (ord(k),))]


def fkeys():  # [(n, label, method)]: what the F-keys do, mc's ten, from the table
    return sorted((int(k[1:]), w.split()[0].rstrip(":"), m) for _, keys, w, m in KEYS if m
                  for k in keys.split() if k[:1] == "F" and k[1:].isdigit())


def menu_rows(m):  # a menu's rows; the two pane menus share theirs
    return [r for r in KEYS if r[0] == ("Pane" if m.startswith("Pane") else m)]


def layout(H, W):  # -> ([(pane or None for the focused one, y, x, h, w)], log rows), or None when too small
    logn = 3 if H >= 18 else 1
    A = H - 3 - logn  # rows for the panes: below the menu bar, above the border, its log and the F-key bar
    side = [(0, 1, 0, A, W // 2), (1, 1, W // 2, A, W - W // 2)]
    stack = [(0, 1, 0, A // 2, W), (1, 1 + A // 2, 0, A - A // 2, W)]
    for rects in (stack, side) if 2 * H > W else (side, stack):  # upright (a cell is about twice as tall as wide)
        if all(h >= PANE_H and w >= PANE_W for *_, h, w in rects):
            return rects, logn
    return ([(None, 1, 0, A, W)], logn) if W >= MIN_W and H >= MIN_H else None


def differ(mine, theirs):  # names here missing there, or (files) of another size or newer here: what a sync copies
    there = dict(theirs)
    return {n for n, a in mine if a and (n not in there or not folder(a) and (
        a.get("size") != there[n].get("size") or a.get("mtime", 0) > there[n].get("mtime", 0)))}


def lsl(name, a):  # the ls -l line of an entry: mode, links, owner, size, time, name -> target
    owner = f"{a.get('user', a.get('uid', '?'))}:{a.get('group', a.get('gid', '?'))}" if "uid" in a else ""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(a["mtime"])) if "mtime" in a else ""
    to = f" -> {a['target']}" if "target" in a else ""
    return " ".join(x for x in (stat.filemode(a.get("perm", 0)), str(a.get("nlink") or ""), owner,
                                human(a.get("size")) if not isdir(a) else "", when, name + to) if x)


class View:  # one list on screen: its rows (after the filter), cursor, scroll and filter
    def __init__(v):
        v.rows, v.sel, v.top, v.flt = [], 0, 0, ""


class Pane:  # a folder on some file system, or the host list to pick one
    def __init__(p):
        p.fs = p.host = p.free = p.found = None  # found: the pattern, while the pane shows find results
        p.cwd, p.items, p.marks, p.closed = "", [], set(), set()
        p.up, p.back, p.fwd = [], [], []  # up: the archives we're inside; back/fwd: (host, folder) history
        p.files, p.hostv, p.mode = View(), View(), "hosts"

    @property
    def v(p):
        return p.files if p.mode == "files" else p.hostv

    def cur(p):  # the row under the cursor
        return p.v.rows[p.v.sel] if p.v.rows else None

    def here(p):  # where the pane is, for history: inside an archive, the folder holding it
        return p.host, p.up[0][1] if p.up else p.cwd

    def remember(p):  # where the pane is, onto its history (b goes back to it); forward is forgotten
        p.back, p.fwd = [*p.back, p.here()][-50:], []

    def targets(p):  # what an action works on: the marked entries, else the one under the cursor
        if p.marks:
            return [(n, a) for n, a in p.items if n in p.marks]
        c = p.cur()
        return [c] if c and c[1] else []


class App:
    def __init__(s, scr, cfg):
        s.cfg, s.scr, s.msg, s.typing = cfg, scr, "", None
        s.panes, s.focus, s.rects, s.single, s.spans, s.opened = [Pane(), Pane()], 0, [], False, {}, None  # opened: menu
        s.local, s.remotes, s.hosts, s.c, s.cmds, s.theme_name = Local(), {}, {}, {}, {}, "default"
        s.dots, s.order, s.rev = True, "name", False
        s.log, s.logoff, s.working, s.spun, s.drawing = deque(maxlen=500), 0, None, 0, False
        curses.curs_set(0)
        curses.set_escdelay(25)
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        if curses.has_colors():
            curses.start_color()
            with contextlib.suppress(curses.error):
                curses.use_default_colors()  # without -1 support, set_theme falls back to mono
        s.keymap = {c: getattr(s, m) for _, keys, _, m in KEYS if m for c in codes(keys)}
        signal.signal(signal.SIGALRM, s.spin)
        atexit.register(s.cleanup)
        s.load()
        LOCAL["_cwd"] = lcwd()
        for p in s.panes:
            p.closed = config.groups(s.hosts)  # folded: only hosts outside any group show
            s.sync(p)

    @property
    def P(s):
        return s.panes[s.focus]

    @property
    def O(s):
        return s.panes[1 - s.focus]

    def folder(s):  # the focused pane, when it shows a folder: what file actions need
        if s.P.mode != "files":
            raise OSError("pick a host first: ⏎ on it")
        return s.P

    def load(s):
        try:
            c = config.read(s.cfg)
            old = {h["host"]: h for *_, h, _ in config.flat(s.hosts, set()) if h}
            s.c, s.hosts, s.cmds = c, config.hosts(c), config.table(c, "commands")
            new = {h["host"]: h for *_, h, _ in config.flat(s.hosts, set()) if h}
            for k, h in new.items():  # a host keeps its last folder, a pane its host, through a reload
                if k in old and "_cwd" in old[k]:
                    h["_cwd"] = old[k]["_cwd"]
            for p in s.panes:
                p.host = new.get(p.host["host"], p.host) if p.host and p.host is not LOCAL else p.host
            s.theme_name = str(c.get("theme", s.theme_name))
        except (OSError, tomllib.TOMLDecodeError) as e:
            s.msg = f"✗ config: {e}"
        s.set_theme(s.theme_name)
        for p in s.panes:
            s.sync(p)

    def set_theme(s, name):  # a theme needing more colours than the terminal has falls back to mono, not garble
        s.theme_name = name if name in THEMES else "default"
        t = THEMES[s.theme_name]
        s.C = MONO
        if curses.has_colors() and max(max(x[:2]) for x in t) < curses.COLORS:
            with contextlib.suppress(curses.error):
                for i, x in enumerate(t, 1):
                    curses.init_pair(i, x[0], x[1])
                s.C = {r: curses.color_pair(i) | STYLE.get(x[2:3] and x[2], 0) for i, (r, x) in enumerate(zip(ROLES, t), 1)}
        s.scr.bkgd(" ", s.C["norm"])

    def sync(s, p):  # each view its own filter. ponytail: a hosts filter unfolds every group; per-match if big trees get noisy
        hq, fq = p.hostv.flt.lower(), p.files.flt.lower()
        p.hostv.rows = [r for r in [(0, "local", LOCAL, ("local",)), *config.flat(s.hosts, set() if hq else p.closed)]
                        if config.match(r, hq)]
        items = sorted(p.items[1:], key=ORDER[s.order], reverse=s.rev)
        items.sort(key=lambda x: not folder(x[1]))  # stable: folders first, the chosen order within
        p.files.rows = [r for r in p.items[:1] + items if r[0] == ".." or fq in r[0].lower()
                        and (s.dots or r[0][:1] != ".")]
        for v in (p.hostv, p.files):  # the cursor stays on a row, shown or not
            v.sel = min(v.sel, max(len(v.rows) - 1, 0))

    # --- processes ---
    def busy(s, what):  # say what's going on (the spinner turns by itself while a call blocks); Esc stops it here,
        if not s.working:  # ^C anywhere
            s.t0 = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, 0.12, 0.12)
        s.working = what
        s.paint()
        s.scr.refresh()
        s.scr.nodelay(True)
        try:
            k = s.scr.getch()
        finally:
            s.scr.nodelay(False)
        if k == 27:
            raise KeyboardInterrupt
        if k != -1:
            curses.ungetch(k)  # typed ahead: it's for after

    def idle(s):
        signal.setitimer(signal.ITIMER_REAL, 0)
        s.working = None

    def spin(s, *_):  # SIGALRM: Python runs this between curses calls, never inside one; skip mid-frame
        if s.working and not s.drawing:
            s.spun += 1
            s.paint_work()
            s.scr.refresh()

    @contextlib.contextmanager
    def still(s):  # no spinner while someone types: prompts, or the terminal handed to ssh/an editor
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            yield
        finally:
            if s.working:
                signal.setitimer(signal.ITIMER_REAL, 0.12, 0.12)

    def tty(s, fn, *a, **k):  # hand the terminal over (prompts, pinentry, editors, shells), take it back
        with s.still():  # spinner off first: a tick after endwin() would drag curses back over the handed-out screen
            curses.endwin()
            sigint = signal.signal(signal.SIGINT, lambda *_: None)
            try:
                return fn(*a, **k)
            finally:
                signal.signal(signal.SIGINT, sigint)
                curses.mousemask(curses.ALL_MOUSE_EVENTS)
                s.scr.clear()
                s.scr.refresh()  # back in curses mode now, so a second handover in a row works

    def term(s, cmd, cwd=None):
        return s.tty(subprocess.call, cmd, cwd=cwd)

    def fsfor(s, h):  # one Remote per destination: both panes on one host share its session
        return s.local if h is LOCAL else s.remotes.setdefault(h["host"], Remote(h, s.tty, s.busy))

    def ticker(s, label, total):  # progress in the activity panel; copies report how far each file is
        t0 = last = time.monotonic()
        pos, moved = {}, 0

        def tick(path, at, resumed=False):  # resumed: bytes found already there, not moved now
            nonlocal moved, last
            step, pos[path] = at - pos.get(path, 0), at
            tick.done += step
            moved += 0 if resumed else step
            if time.monotonic() - last > 0.25:
                last, rate = time.monotonic(), moved / max(time.monotonic() - t0, 1e-3)
                left = f" {tick.done * 100 // total}%  {int((total - tick.done) / rate) if rate else '?'}s left" if total else ""
                files = f"  {len(tick.finished)} files" if tick.finished else ""
                s.busy(f"{label}  {human(tick.done)}{f' of {human(total)}' if total else ''}{left}  {human(rate)}/s{files}"
                       "  Esc stops")
        tick.done, tick.finished, tick.skipped = 0, set(), []
        return tick

    def xfer(s, fn, label, total, fss):  # fn(tick) over file systems fss; a dropped link reconnects and resumes
        tick, fails, seen, t0 = s.ticker(label, total), 0, 0, time.monotonic()
        s.busy(label)
        while True:
            try:
                fn(tick)
                break
            except OSError as e:
                if all(f.alive() for f in fss) and not unreachable(str(e)):
                    raise  # a real refusal (permissions, disk full, wrong password): retrying won't help
                err = e
            fails, seen = (1 if tick.done > seen else fails + 1), tick.done  # progress since the last drop resets it
            if fails > 5:
                raise err
            s.msg = f"✗ {label}: link lost ({err})"
            s.busy(f"{label}: reconnecting in {min(2 ** fails, 20)}s, then resuming")
            time.sleep(min(2 ** fails, 20))
        n = f", {len(tick.finished)} files" if len(tick.finished) > 1 else ""
        s.msg = f"✓ {label}  {human(tick.done)}{n} in {time.monotonic() - t0:.0f}s"

    # --- moving around ---
    def ls(s, p, d, keep=None, record=True):  # list folder d in pane p; keep: a name for the cursor; record: history
        s.busy(f"listing {safe(d) or '~'}")
        d = p.fs.realpath(d)
        items = look(p.fs, d)
        if d != p.cwd or p.found:
            if record and p.cwd and not p.up:
                p.remember()
            p.marks, p.files.flt, p.files.sel, p.files.top, p.found = set(), "", 0, 0, None  # a fresh start
        p.cwd, p.items = d, [("..", None), *items]
        with contextlib.suppress(OSError):
            p.free = p.fs.free(d)
        if not p.up:
            p.host["_cwd"] = d
        s.sync(p)
        if keep:
            p.files.sel = next((i for i, r in enumerate(p.files.rows) if r[0] == keep), p.files.sel)

    def relist(s):  # both panes, after anything that may have changed what they show; find results keep only
        for p in s.panes:  # what's still there
            if p.found:
                p.items = [(n, a) for n, a in p.items if not a or peek(p.fs, join(p.cwd, n))]
                s.sync(p)
            elif p.fs:
                s.ls(p, p.cwd)

    def open_host(s, p, h, d=None, record=True):  # pane p shows host h (its last folder, else d, else its path)
        while p.up:
            s.leave(p)
        was = p.fs, p.host, p.mode, p.cwd
        if record and p.cwd and (h is not p.host or d not in (None, p.cwd)):  # a move: back comes here
            p.remember()
        p.fs, p.host, p.mode, p.cwd = s.fsfor(h), h, "files", ""
        try:
            s.ls(p, d if d is not None else h.get("_cwd") or h.get("path", ""), record=False)
        except BaseException:
            p.fs, p.host, p.mode, p.cwd = was  # couldn't get in: the pane stays as it was
            raise

    def open(s):  # ⏎: folder in, file by its kind; on the host list: open the host here, or fold the group
        p = s.P
        c = p.cur()
        if not c:
            return
        if p.mode == "hosts":
            if c[2]:
                return s.open_host(p, c[2])
            p.closed ^= {c[3]}
            return s.sync(p)
        if c[0] == "..":
            return s.parent()
        if p.found:  # a find result: to its folder, the cursor on it
            d, _, n = c[0].rpartition("/")
            return s.ls(p, join(p.cwd, d) if d else p.cwd, keep=n)
        if folder(c[1]):
            return s.ls(p, join(p.cwd, c[0]))
        s.launch("open")

    def parent(s):  # up a folder (out of an archive at its top; from find results back to the folder)
        p = s.P
        if p.mode == "hosts":
            c = p.cur()
            if c and not c[2] and c[3] not in p.closed:
                p.closed.add(c[3])
                s.sync(p)
            elif c and len(c[3]) > 1:
                p.hostv.sel = next((i for i, r in enumerate(p.hostv.rows) if r[3] == c[3][:-1]), p.hostv.sel)
        elif p.found:
            s.ls(p, p.cwd)
        elif p.up and p.cwd == "/":
            s.ls(p, *s.leave(p))
        else:
            s.ls(p, p.cwd.rsplit("/", 1)[0] or "/", keep=p.cwd.rsplit("/", 1)[-1])

    def fetch(s, p, name, a):  # a private copy (0700 folder) of a file not on this machine -> (folder, file);
        d = tempfile.mkdtemp()  # a link: what it points to, not a copy of the link
        lp = os.path.join(d, name.rsplit("/", 1)[-1])
        try:
            s.xfer(lambda t: copy(p.fs, join(p.cwd, name), s.local, lp, t, follow=True), f"↓ {name}",
                   (a.get("dest") or a).get("size"), [p.fs])
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return d, lp

    def enter(s, p, name, a):  # an archive, browsed like a folder; one not on this machine comes over first
        d, lp = (None, join(p.cwd, name)) if isinstance(p.fs, Local) else s.fetch(p, name, a)
        try:
            s.busy(f"reading {name}")
            ar = Archive(lp, f"{p.fs.name}:{join(p.cwd, name)}")
        except BaseException:
            d and shutil.rmtree(d, ignore_errors=True)
            raise
        if not p.up:
            p.remember()
        p.up.append((p.fs, p.cwd, name, d))
        p.fs, p.cwd = ar, ""
        s.ls(p, "/")

    def leave(s, p):  # out of the archive we're in (its copy goes) -> (the folder it is in, its name)
        p.fs.close()
        p.fs, p.cwd, name, d = p.up.pop()
        p.marks, p.files.flt = set(), ""  # names marked inside mean nothing outside
        if d:
            shutil.rmtree(d, ignore_errors=True)
        return p.cwd, name

    def cleanup(s):  # at exit: no archive copies left behind
        for p in s.panes:
            while p.up:
                s.leave(p)

    def hostlist(s):  # @, or a tap on the title: this pane to the host list and back
        p = s.P
        if p.mode == "files":
            p.mode = "hosts"
            s.sync(p)
            p.hostv.sel = next((i for i, r in enumerate(p.hostv.rows) if r[2] is p.host), p.hostv.sel)
        elif p.fs:
            p.mode = "files"

    def pick(s, p, path):  # select the host at path in p's list, unfolding the groups around it
        p.closed -= {path[:i] for i in range(1, len(path))}
        s.sync(p)
        p.hostv.sel = next(i for i, r in enumerate(p.hostv.rows) if r[3] == path)

    def back(s):
        s.history(s.folder().back, s.P.fwd)

    def forward(s):
        s.history(s.P.fwd, s.P.back)

    def history(s, src, dst):  # one step through a pane's history: the folder there, on its host
        p = s.folder()
        if not src:
            raise OSError("no further")
        h, d = src.pop()
        dst.append(p.here())
        s.open_host(p, h, d, record=False) if h is not p.host or p.up else s.ls(p, d, record=False)

    def path(s, p, x):  # a typed path: as is from / or ~, else from the pane's folder
        return x if x[:1] in ("/", "~") else join(p.cwd, x)

    def goto(s):
        p = s.folder()
        d = s.ask("go to:", p.cwd)
        if d:
            s.ls(p, s.path(p, d))

    def home(s):
        s.ls(s.folder(), "")

    def mirror(s):  # the other pane to this folder
        p = s.folder()
        if p.up:
            raise OSError("not from inside an archive")
        s.open_host(s.O, p.host, p.cwd)

    def swap(s):
        s.panes.reverse()
        s.focus ^= 1

    def tab(s):
        s.focus ^= 1

    def find(s):  # ^F: names like a pattern, anywhere below this folder; the results become the pane's listing
        p = s.folder()
        pat = s.ask("find names like:", "*")
        if not pat:
            return
        found = []
        try:
            for rel, a in walk(p.fs, p.cwd):
                if fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pat):
                    found.append((rel, a))
                    if len(found) % 50 == 0:
                        s.busy(f"find {pat}: {len(found)}  Esc stops")
        except KeyboardInterrupt:
            s.msg = f"✗ find stopped: {len(found)} so far"
        p.items, p.found, p.marks, p.files.sel, p.files.top = [("..", None), *found], pat, set(), 0, 0
        s.sync(p)
        s.msg = s.msg or f"✓ {len(found)} like {pat}: Esc goes back"

    def search(s):  # filter this view: type to narrow, arrows move; ⏎ applies (never opens), Esc clears
        p = s.P
        v = p.v
        s.typing, v.flt = "/", ""
        curses.curs_set(1)
        try:
            while True:
                s.paint()
                try:
                    c = s.scr.get_wch()
                except curses.error:
                    continue
                if c in ("\n", "\r", "\t", curses.KEY_ENTER):
                    break
                if c == "\x1b":
                    v.flt = ""
                elif isinstance(c, int) and c in MOVES:
                    s.step(MOVES[c])
                    continue
                elif c in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    v.flt = v.flt[:-1]
                elif isinstance(c, str) and c.isprintable():
                    v.flt += c
                else:
                    continue
                s.sync(p)
                v.sel = int(p.mode == "files" and len(v.rows) > 1)  # first match, not the ever-present ..
                if c == "\x1b":
                    break
        finally:
            s.typing = None
            curses.curs_set(0)

    def sort(s):
        i = s.choose([(o, "✓" if o == s.order else "") for o in ORDER] + [("reverse", "✓" if s.rev else "")],
                     title="sort")
        if isinstance(i, int):
            if i == len(ORDER):
                s.rev = not s.rev
            else:
                s.order = list(ORDER)[i]
            for p in s.panes:
                s.sync(p)
            s.msg = f"sorted by {s.order}{', reversed' if s.rev else ''}"

    def dotfiles(s):
        s.dots = not s.dots
        for p in s.panes:
            s.sync(p)
        s.msg = f"dotfiles {'shown' if s.dots else 'hidden'}"

    def reload(s):
        s.load()
        for p in s.panes:
            if p.fs:
                s.ls(p, p.cwd)

    def esc(s):  # back out of one thing: the filter, else the marks, else find results, else the host list
        p = s.P
        if p.v.flt:
            p.v.flt = ""
            s.sync(p)
        elif p.mode == "files" and p.marks:
            p.marks = set()
        elif p.mode == "files" and p.found:
            s.ls(p, p.cwd)
        elif p.mode == "hosts" and p.fs:
            p.mode = "files"

    def step(s, d):  # the cursor, d rows down (up if negative), kept inside the list
        v = s.P.v
        v.sel = max(0, min(len(v.rows) - 1, v.sel + d))

    # --- marks ---
    def mark(s):
        p = s.folder()
        c = p.cur()
        if c and c[1]:
            p.marks ^= {c[0]}
            s.step(1)

    def markall(s):
        p = s.folder()
        p.marks = set() if p.marks else {n for n, a in p.files.rows if a}

    def markpat(s, plus=True):  # every shown entry whose name matches a pattern like *.log
        p = s.folder()
        pat = s.ask(f"{'mark' if plus else 'unmark'} names like:", "*")
        hit = {n for n, a in p.files.rows if a and fnmatch.fnmatchcase(n, pat)} if pat else set()
        p.marks = p.marks | hit if plus else p.marks - hit

    def unmarkpat(s):
        s.markpat(False)

    def compare(s):  # D: marks what's missing in the other pane, or (files) differs in size or is newer here
        p, o = s.folder(), s.O
        if o.mode != "files":
            raise OSError("the other pane shows no folder")
        p.marks = differ(p.items, o.items)
        s.msg = f"{len(p.marks)} differ: F5 copies them over" if p.marks else "✓ nothing missing or newer here"

    # --- files ---
    def info(s):  # i: everything about the entry under the cursor, in the viewer (where a long name wraps whole)
        p = s.folder()
        c = p.cur()
        if not c or not c[1]:
            return
        n, a = c
        when = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        rows = [("name", n), ("in", f"{p.fs.name}:{p.cwd}"), ("kind", kind(n, a)),
                ("mode", f"{stat.filemode(a.get('perm', 0))}  {stat.S_IMODE(a.get('perm', 0)):04o}"),
                ("links", f"{a['nlink']}: the same file under {a['nlink']} names (hard links)"
                 if (a.get("nlink") or 0) > 1 and not isdir(a) and not islink(a) else a.get("nlink")),
                ("owner", f"{a.get('user', '?')} ({a.get('uid', '?')})" if "uid" in a else None),
                ("group", f"{a.get('group', '?')} ({a.get('gid', '?')})" if "gid" in a else None),
                ("size", f"{a.get('size', 0)} bytes  {human(a.get('size', 0))}"),
                ("modified", when(a["mtime"]) if "mtime" in a else None), ("points to", a.get("target")),
                ("free there", human(p.free) if p.free is not None else None)]
        with s.still():
            viewer.view(s.scr, "\n".join(f"{k:>10}  {v}" for k, v in rows if v is not None).encode(), f"info: {n}", s.C)

    def view(s):
        s.launch("view")

    def edit(s):  # on the host list: the config
        if s.P.mode == "hosts":
            return s.editcfg()
        s.launch("edit")

    def launch(s, how):  # ⏎ ("open"), view or edit the file under the cursor: your [open] pattern if one matches;
        p = s.folder()     # text to your tools; anything else to rshm's own viewer (its first MB) or editor (up to a MB)
        c = p.cur()
        if not c or not c[1] or folder(c[1]):
            return
        name, a = c
        path = join(p.cwd, name)
        if special(a.get("dest") or a):
            raise OSError(f"{name}: a pipe, socket or device; nothing to open")
        cmd = config.opener(s.c, name) if how == "open" else None
        if cmd:
            return s.external(p, name, a, cmd, False)
        if how == "open" and name.lower().endswith(ARCHIVES):
            return s.enter(p, name, a)
        size = p.fs.stat(path).get("size", 0)  # as it is now, not as listed
        tool = config.tool(s.c, "edit" if how == "edit" else "view")
        if how == "edit" and not tool and size > HEAD:
            raise OSError(f"{name} is {human(size)}: more than rshm's editor takes ({human(HEAD)}); [tools] edit can")
        data = s.contents(p, path, min(size, 8192 if tool else HEAD))  # enough to tell text from binary, or all it shows
        if b"\0" in data[:8192]:  # binary: never to an editor, never to a text tool
            if how == "edit":
                raise OSError(f"{name} is binary: v shows it")
            tool = None
        if tool:
            return s.external(p, name, a, tool, how == "edit")
        data += s.contents(p, path, min(size, HEAD), len(data))
        s.idle()
        title = f"{p.fs.name}:{path}" + (f"  (the first {human(HEAD)} of {human(size)})" if size > HEAD else "")
        with s.still():
            if how != "edit":
                viewer.view(s.scr, data, title, s.C)
            else:
                editor.edit(s.scr, data, title, s.C, lambda b: save(p.fs, path, b))
        if how == "edit":
            s.relist()

    def contents(s, p, path, end, off=0):  # bytes off..end of a file, read with progress; Esc stops it
        s.busy(f"loading {posixpath.basename(path)}")
        tick, out, n = s.ticker(f"loading {posixpath.basename(path)}", end - off), [], 0
        for b in p.fs.read(path, off, end):
            out.append(b)
            n += len(b)
            tick(path, n)
        return b"".join(out)

    def external(s, p, name, a, template, edit):  # your tool, on a local file: a private copy of anything else
        path = join(p.cwd, name)
        if isinstance(p.fs, Local):
            s.term(["sh", "-c", config.cmdline(template, path)], p.cwd)
            return s.relist()
        d, lp = s.fetch(p, name, a)
        keep = False  # the copy is removed afterwards, unless it holds an edit that couldn't be saved
        try:
            t0 = os.stat(lp).st_mtime_ns
            s.term(["sh", "-c", config.cmdline(template, lp)], d)
            if edit and os.stat(lp).st_mtime_ns != t0:
                try:
                    s.xfer(lambda t: copy(s.local, lp, p.fs, path, t), f"↑ {name}", os.path.getsize(lp), [p.fs])
                except BaseException as e:
                    keep = True
                    raise OSError(f"not saved ({e or 'stopped'}): your edit is in {lp}") from None
                s.msg = f"✓ saved {name}"
                s.relist()
        finally:
            if not keep:
                shutil.rmtree(d, ignore_errors=True)

    def copy(s):
        s.transfer(move=False)

    def move(s):
        s.transfer(move=True)

    def transfer(s, move):  # the marked entries (or the one under the cursor) to the other pane's folder
        p, o = s.folder(), s.O
        items = p.targets()
        if not items:
            return
        if o.mode != "files":
            raise OSError("the other pane shows no folder: pick one there (Tab, then ⏎ on a host)")
        if move and isinstance(p.fs, Archive):
            raise OSError("an archive is read-only: copy out of it (F5) instead")
        what = items[0][0] if len(items) == 1 else f"{len(items)} entries"
        dst = s.ask(f"{'move' if move else 'copy'} {what} to {o.fs.name}:", o.cwd)
        if not dst:
            return
        dst = dst if dst.startswith("/") else join(o.cwd, dst)
        at = peek(o.fs, dst)
        if at and islink(at):
            at = peek(o.fs, o.fs.realpath(dst))
        if at and isdir(at):  # into that folder (find results land flat, by their own names)
            there = dict(o.fs.listdir(dst))
            pairs = [(n, a, join(dst, n.rsplit("/", 1)[-1])) for n, a in items]
        elif len(items) == 1 and not at:  # a new name for the one entry
            there, pairs = {}, [(items[0][0], items[0][1], dst)]
        else:
            raise OSError(f"{dst}: not a folder")
        same = o.fs is p.fs
        if same:  # by real paths: "a/../b" or a link into the tree mustn't get past these
            real = lambda t: join(o.fs.realpath(posixpath.dirname(t)), posixpath.basename(t))
            src = {n: p.fs.realpath(join(p.cwd, n)) for n, _, _ in pairs}
            pairs = [x for x in pairs if real(x[2]) != src[x[0]]]  # onto itself: nothing to do
            if any(real(t).startswith(src[n] + "/") for n, _, t in pairs):
                raise OSError("a folder can't go inside itself")
        if not pairs:
            raise OSError("that is where it already is")
        if len({t for *_, t in pairs}) < len(pairs):  # find results from different folders, one name
            raise OSError("two of these have the same name: they'd land on each other")
        clash = {n for n, _, t in pairs if t.rsplit("/", 1)[-1] in there}
        if clash and not s.confirm(f"{len(clash)} already there ({min(clash)}…): overwrite, merging folders? (y/N)"):
            return
        kept, names = [], {n for n, _, _ in pairs}  # kept: moved trees holding pipes, sockets or devices stay
        try:
            if move and same:  # one file system: a rename, nothing travels; only a folder merge needs copies
                for n, a, t in [x for x in pairs if not (x[0] in clash and isdir(x[1]))]:
                    s.busy(f"moving {n}")
                    p.fs.rename(join(p.cwd, n), t)
                    pairs.remove((n, a, t))
            if pairs:
                total = None if any(isdir(a) for _, a, _ in pairs) else sum(a.get("size", 0) for _, a, _ in pairs if not islink(a))
                gone = set()  # moved entries already removed at the source: a retry skips them

                def run(tick):
                    for n, a, t in pairs:
                        if n not in gone:
                            skips = len(tick.skipped)
                            copy(p.fs, join(p.cwd, n), o.fs, t, tick)
                            if move and len(tick.skipped) > skips:
                                kept.append(n)
                            elif move:
                                rm(p.fs, join(p.cwd, n))
                            gone.add(n)
                s.xfer(run, f"{'→' if s.focus == 0 else '←'} {what}", total, [p.fs, o.fs])
            s.msg = s.msg or f"✓ moved {what}"
            if kept:
                s.msg += f"; kept {', '.join(kept)} here: pipes, sockets or devices in it don't travel"
        finally:
            p.marks -= names
            s.relist()

    def mkdir(s):
        p = s.folder()
        name = s.ask("new folder:").strip("/")
        if name:
            p.fs.mkdir(join(p.cwd, name))
            s.ls(p, p.cwd, keep=name.split("/")[0])

    def delete(s):
        p = s.folder()
        items = p.targets()
        what = items[0][0] + ("/" if isdir(items[0][1]) else "") if len(items) == 1 else f"{len(items)} entries"
        if items and s.confirm(f"delete {what}? (y/N)"):
            try:
                for n, _ in items:
                    s.busy(f"deleting {n}")
                    rm(p.fs, join(p.cwd, n))
                    p.marks.discard(n)
                s.msg = f"✓ deleted {what}"
            finally:
                s.relist()  # show what's left even if it stopped halfway

    def rename(s):  # never overwrites; "sub/", "sub/x" or "/abs/x" move it on this file system, like mv
        p = s.folder()
        c = p.cur()
        if not c or not c[1]:
            return
        new = s.ask("rename to:", c[0])
        if new and new != c[0]:
            dst = new if new.startswith("/") else join(p.cwd, new)
            dst = join(dst, c[0]) if new.endswith("/") else dst
            p.fs.rename(join(p.cwd, c[0]), dst, replace=False)
            s.ls(p, p.cwd, keep=new)
            s.msg = f"✓ renamed {c[0]} to {new}"

    def symlink(s):  # L: a symlink to the entry under the cursor, where you say; on a link: where it points, changed
        p = s.folder()
        c = p.cur()
        if c and c[1] and islink(c[1]):
            path = join(p.cwd, c[0])
            to = s.ask("points to:", c[1]["target"])
            if to and to != c[1]["target"]:  # the new link beside the old, then over it: never neither
                p.fs.symlink(to, path + PART)
                p.fs.rename(path + PART, path)
                s.msg = f"✓ {c[0]} -> {to}"
                s.relist()
        else:
            s.link("symlink")

    def hardlink(s):  # H: another name for the file under the cursor, where you say
        s.link("hard link")

    def link(s, kind):  # a new name at a path you type (the other pane's folder offered) for the entry under the cursor
        p = s.folder()
        c = p.cur()
        if not c or not c[1]:
            return
        o, name, src = s.O, posixpath.basename(c[0]), join(p.cwd, c[0])
        here = o.cwd if o.mode == "files" and o.fs is p.fs and not o.found else p.cwd  # one file system only
        at = s.ask(f"{kind} to {name} at:", join(here, name))
        if not at:
            return
        at = s.path(p, at)
        at = join(p.fs.realpath(posixpath.dirname(at)), posixpath.basename(at))
        if kind == "symlink":  # relative: it still points right when the tree moves or is mounted elsewhere
            to = posixpath.relpath(src, posixpath.dirname(at))
            p.fs.symlink(to, at)
            s.msg = f"✓ {at} -> {to}"
        else:
            p.fs.hardlink(src, at)
            s.msg = f"✓ {at}: another name for {name}"
        s.relist()

    def owner_id(s, p, key, name):  # a user or group name as its number: on a host, as its files show it
        what = {"uid": "user", "gid": "group"}[key]
        if name.isdigit():
            return int(name)
        found = next((a[key] for _, a in p.items if a and a.get(what) == name and key in a), None)
        if found is None and isinstance(p.fs, Local):
            with contextlib.suppress(KeyError):
                found = pwd.getpwnam(name).pw_uid if key == "uid" else grp.getgrnam(name).gr_gid
        if found is None:
            raise OSError(f"{name}: no {what} by that name here; its number works")
        return found

    def chattr(s):  # A: mode, owner, time of the marked entries (or the one under the cursor); what you change, changes
        p = s.folder()
        items = p.targets()
        if not items:
            return
        a = items[0][1]
        fields = [  # (label, shown, read back into changes): one row per attribute; ACLs or labels would be one more
            ("mode", f"{stat.S_IMODE(a.get('perm', 0)):o}", lambda v: {"perm": int(v, 8)}),
            ("owner", f"{a.get('user', a.get('uid', ''))}:{a.get('group', a.get('gid', ''))}",
             lambda v: {k: s.owner_id(p, k, n) for k, n in zip(("uid", "gid"), v.partition(":")[::2]) if n}),
            ("modified", time.strftime("%Y-%m-%d %H:%M", time.localtime(a.get("mtime", 0))),
             lambda v: {"mtime": int(time.mktime(time.strptime(v, "%Y-%m-%d %H:%M")))})]
        new = s.form("attributes", [(label, shown) for label, shown, _ in fields])
        ch, done = {}, []
        for (label, shown, read), v in zip(fields, new or ()):
            if v.strip() != shown:
                try:
                    ch |= read(v.strip())
                except ValueError:
                    raise OSError(f"{label} {v.strip()}: not like {shown}") from None
                done.append(f"{label} {v.strip()}")
        if ch:
            for n, _ in items:
                p.fs.chattr(join(p.cwd, n), ch)
            s.relist()
            s.msg = f"✓ {', '.join(done)}"

    def size(s):  # z: the size of the marked entries, or of the one under the cursor (a folder: all inside it)
        p = s.folder()
        items = p.targets()
        total = files = 0
        for n, a in items:
            total, files = total + a.get("size", 0) * (not isdir(a)), files + (not isdir(a))
            if isdir(a):
                for i, (_, b) in enumerate(walk(p.fs, join(p.cwd, n))):
                    if not isdir(b):
                        total, files = total + b.get("size", 0), files + 1
                    if i % 200 == 0:
                        s.busy(f"size {n}: {human(total)}  Esc stops")
        if items:
            s.msg = f"{items[0][0] if len(items) == 1 else f'{len(items)} entries'}: {human(total)} in {files} files"

    # --- commands ---
    def menu(s, y=None, x=None):  # F2, or a right-click: what can be done with what's under the cursor, your
        p = s.P                       # [commands] too (run in this pane's folder)
        acts = [r for r in KEYS if r[3] in ("open", "shell", "ping", "edit")] if p.mode == "hosts" else menu_rows("File")
        cmds = list(s.cmds) if p.mode == "files" else []
        c = p.cur()
        i = s.choose([(w, k) for _, k, w, _ in acts] + [(f"$ {n}", "") for n in cmds], y, x,
                     title=(c[1] if p.mode == "hosts" else c[0]) if c else "")
        if not isinstance(i, int):
            return
        if i < len(acts):
            return s.act(getattr(s, acts[i][3]))
        q = shlex.quote
        sub = {"{name}": q(c[0]) if c and c[1] else "", "{names}": " ".join(q(n) for n, _ in p.targets()),
               "{dir}": q(p.cwd), "{other}": q(s.O.cwd)}
        cmd = s.cmds[cmds[i - len(acts)]]
        for k, v in sub.items():
            cmd = cmd.replace(k, v)
        s.run(p.fs, f"{cmd}; printf '\\n[exit %s] ⏎ ' $?; read _", p.cwd)

    def run(s, fs, script, d):  # a shell command on that machine, on the terminal; then show what changed
        cmd, cwd = fs.run(script, d)
        s.term(cmd, cwd)
        s.relist()

    def shell(s):  # ^O: a shell in this folder; on the host list, on the host under the cursor
        p = s.P
        if p.mode == "files":
            return s.run(p.fs, SHELL, p.cwd)
        c = p.cur()
        if c and c[2]:
            s.run(s.fsfor(c[2]), SHELL, c[2].get("_cwd") or c[2].get("path", ""))

    def ping(s):  # p: one round trip to the host, by a session's route (jumps too); never asks for a login
        p = s.P
        h = p.host if p.mode == "files" else (p.cur() or (0, 0, None))[2]
        if not h or h is LOCAL:
            return
        try:
            connect(h, s.tty, s.busy, timeout=3, ask=False)
        except ConnectionError as e:
            raise OSError(f"{h['host']} unreachable: {e}" if unreachable(str(e)) else
                          f"{h['host']} answers, but wants a login: ⏎ on it logs in") from None
        s.busy(f"live check {h['host']}")
        t0 = time.monotonic()
        rc, err = quiet([*argv(h, batch=True, timeout=3), h["host"], "true"])
        if rc:
            raise OSError(f"{h['host']}: {err}")
        s.msg = f"✓ {h['host']} answers ({(time.monotonic() - t0) * 1000:.0f} ms)"

    # --- options ---
    def theme(s):
        names = list(THEMES)
        s.set_theme(names[(names.index(s.theme_name) + 1) % len(names)])
        if s.cfg.exists():  # no config: for this session only, nothing written
            config.save(s.cfg, config.retheme(s.cfg.read_text(), s.theme_name))
        s.msg = f"theme: {s.theme_name}" + (" (mono: the terminal lacks its colours)" if s.C is MONO else "")

    def editcfg(s):  # the config (from the sample, the first time) in your editor or rshm's own
        config.create(s.cfg)
        cmd = config.tool(s.c, "edit")
        if cmd:
            s.term(["sh", "-c", config.cmdline(cmd, str(s.cfg))])
        else:
            with s.still():
                editor.edit(s.scr, s.cfg.read_bytes(), str(s.cfg), s.C, lambda b: save(s.local, str(s.cfg), b))
        s.load()

    def logscroll(s, d=None):  # [ older, ] newer (and the wheel over the log)
        d = d if d is not None else -1 if s.key_hit == ord("]") else 1
        s.logoff = max(0, min(len(s.log) - 1, s.logoff + d))

    def help(s, y=None, x=None):  # every key; ⏎ runs the chosen one
        i = s.choose([(w, k) for _, k, w, _ in KEYS], y, x, title="keys")
        if isinstance(i, int) and KEYS[i][3]:
            s.act(getattr(s, KEYS[i][3]))

    def menubar(s, i=0):  # F9, or a tap on the bar: one menu open at a time, lit in the bar; ←→ to the next,
        try:              # ↑↓ ⏎ in it; Esc, or a tap anywhere else, closes it
            while i is not None:
                s.opened, rows = i, menu_rows(MENUS[i])
                x = next((x0 for x0, _, j in s.spans.get("menu", []) if j == i), 0)
                r = s.choose([(w, k) for _, k, w, _ in rows], 1, x, nav=True)
                if r in ("left", "right"):
                    i = (i + (1 if r == "right" else -1)) % len(MENUS)
                elif isinstance(r, tuple):  # a tap outside: another menu of the bar, or nothing
                    i = next((j for x0, x1, j in s.spans["menu"] if x0 <= r[1] < x1), None) if r[0] == 0 else None
                else:
                    break
        finally:
            s.opened = None
        if isinstance(r, int):
            if MENUS[i].startswith("Pane"):
                s.focus = int(MENUS[i][-1]) - 1
            s.act(getattr(s, rows[r][3]))

    def quit(s):
        sys.exit()

    # --- prompts ---
    def ask(s, q, text=""):  # the one-line input at the bottom, over the current screen
        with s.still():
            return (readline(s.scr, q, text, s.C["acc"] | curses.A_BOLD, s.paint) or "").strip()

    def confirm(s, q):  # one key: y says yes, anything else no
        s.typing = q
        try:
            with s.still():
                s.paint()
                return s.scr.getch() in (ord("y"), ord("Y"))
        except KeyboardInterrupt:
            return False
        finally:
            s.typing = None

    def anchor(s):  # where a popup opens: at the cursor's row
        v = s.P.v
        _, py, px, _, _ = next((r for r in s.rects if r[0] == s.focus), (0, 1, 0, 0, 0))
        return py + v.sel - v.top + 1, px + 2

    def popup(s, items, i, y0, x0, title=""):  # (label, right) rows framed over a fresh frame, row i lit
        s.paint()  # one overlay over a fresh screen each time, placed for the size it has now: no leftovers
        s.scr.noutrefresh()  # -> (win, y, x, w, h, off)
        H, W = s.scr.getmaxyx()
        w = min(max(max(cells(l) + cells(k) for l, k in items) + 6, cells(title) + 6), W)
        h = min(len(items) + 2, H)
        y, x = max(0, min(y0, H - h)), max(0, min(x0, W - w))
        win = curses.newwin(h, w, y, x)
        win.keypad(True)
        win.bkgd(" ", s.C["norm"])
        off = max(0, i - h + 3)  # scroll when the terminal is shorter than the menu
        with contextlib.suppress(curses.error):  # a terminal too small for a frame: what fits shows
            top = f"─ {title} " if title else ""
            win.addstr(0, 0, "┌" + top + "─" * (w - 2 - cells(top)) + "┐", s.C["acc"])
            if title:
                win.addstr(0, 2, f" {title} ", s.C["acc"] | curses.A_BOLD)
            for r, (l, k) in enumerate(items[off:off + h - 2]):
                win.addstr(r + 1, 0, "│", s.C["acc"])
                win.addstr(r + 1, 1, bar(f" {l}", f"{k} ", w - 1), s.C["sel" if r + off == i else "norm"])
                win.addstr(r + 1, w - 1, "│", s.C["acc"])
            win.addstr(h - 1, 0, "└" + "─" * (w - 2), s.C["acc"])
            win.insstr(h - 1, w - 1, "┘", s.C["acc"])  # the very last cell: written without moving past it
        win.noutrefresh()
        curses.doupdate()
        return win, y, x, w, h, off

    def form(s, title, rows):  # (label, value) rows to edit: ↑ ↓ Tab between them, ⏎ takes all, Esc none -> values
        vals, i, (y0, x0) = [v for _, v in rows], 0, s.anchor()
        while True:
            items = [(l, v) for (l, _), v in zip(rows, vals)]
            with s.still():
                vals[i], k = line(s.scr, f"{title} {rows[i][0]}:", vals[i], s.C["acc"] | curses.A_BOLD,
                                  lambda: s.popup(items, i, y0, x0, title), (curses.KEY_UP, curses.KEY_DOWN, "\t"))
            if k in ("\n", "\x1b"):
                return vals if k == "\n" else None
            i = (i + (-1 if k == curses.KEY_UP else 1)) % len(rows)

    def choose(s, items, y=None, x=None, nav=False, title=""):  # a popup of (label, keys) -> index, None, or with
        i, (y0, x0) = 0, (y, x) if y is not None else s.anchor()   # nav: "left"/"right", or (y, x) of a tap outside it
        while True:
            win, y, x, w, h, off = s.popup(items, i, y0, x0, title)
            c = win.getch()
            if c == curses.KEY_RESIZE:
                continue
            if c in MOVES:
                i = max(0, min(len(items) - 1, i + MOVES[c])) if abs(MOVES[c]) > 1 else (i + MOVES[c]) % len(items)
            elif c in (10, 13, 32, curses.KEY_ENTER):
                return i
            elif nav and c in (curses.KEY_LEFT, curses.KEY_RIGHT):
                return "left" if c == curses.KEY_LEFT else "right"
            elif c == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, b = curses.getmouse()
                except curses.error:
                    continue
                if b & (WHEEL_UP | WHEEL_DOWN):
                    i = max(0, min(len(items) - 1, i + (-1 if b & WHEEL_UP else 1)))
                elif b & (CLICK | RIGHT):
                    r = my - y - 1
                    if x <= mx < x + w and 0 <= r < h - 2 and r + off < len(items):
                        return r + off
                    return (my, mx) if nav else None
            else:
                return None

    # --- input ---
    def key(s, c):
        s.key_hit = c  # for an action on two keys ([ and ])
        if c == curses.KEY_MOUSE:
            s.mouse()
        elif c in MOVES:
            s.step(MOVES[c])
        elif fn := s.keymap.get(c):
            fn()

    def mouse(s):  # every tap lands where it looks like it would: bar, menus, titles, rows, the log
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return
        H, _ = s.scr.getmaxyx()
        tap, wheel = b & (CLICK | RIGHT), (-3 if b & WHEEL_UP else 3) if b & (WHEEL_UP | WHEEL_DOWN) else 0
        if y == 0 and tap:
            i = next((j for x0, x1, j in s.spans.get("menu", []) if x0 <= x < x1), None)
            return s.menubar(i) if i is not None else None
        if y == H - 1 and tap:
            m = next((m for x0, x1, m in s.spans.get("fbar", []) if x0 <= x < x1), None)
            return m and getattr(s, m)()
        hit = next(((i, py, h) for i, py, px, h, w in s.rects if py <= y < py + h and px <= x < px + w), None)
        if not hit:
            if wheel and y > max(r[1] + r[3] for r in s.rects):  # over the log
                s.logscroll(-wheel // 3)
            elif b & RIGHT:
                s.help(y, x)
            return
        i, py, h = hit
        s.focus = i
        p = s.P
        if wheel:
            return s.step(wheel)
        if y == py:  # the title: a tap toggles the host list, right-click opens this pane's menu
            return s.hostlist() if b & CLICK else s.menubar(MENUS.index(f"Pane {i + 1}"))
        j = p.v.top + y - py - 1
        if j >= len(p.v.rows) or not tap:
            return
        again, p.v.sel = j == p.v.sel, j
        if b & RIGHT:
            return s.menu(y, x)
        if again:  # a tap selects, the second opens: a host (a login) too, never by a stray tap
            s.open()

    # --- drawing ---
    def draw(s):  # ^L: the whole screen drawn again, whatever wrote over it
        s.scr.clear()

    def paint(s):  # this frame, from the state; the spinner keeps off while it's drawn
        s.drawing = True
        try:
            s.frame()
        finally:
            s.drawing = False

    def paint_work(s):  # the live line at the bottom of the activity panel: spinner, what, how long
        H, W = s.scr.getmaxyx()
        if s.working and layout(H, W):
            line = f" {SPIN[s.spun % len(SPIN)]} {s.working}  {time.monotonic() - s.t0:.0f}s"
            with contextlib.suppress(curses.error):
                s.scr.addstr(H - 2, 0, fit(line, W - 1), s.C["acc"] | curses.A_BOLD)

    def title(s, i, p, w):  # which pane, and what it shows: its host and folder, the host list, an archive, a find
        where = "hosts" if p.mode == "hosts" else f"find {p.found} in {p.fs.name}:{p.cwd}" if p.found \
            else f"{p.fs.name}:{p.cwd}"
        right = "".join(x for x in (f" [{len(p.marks)}]" if p.marks and p.mode == "files" else "",
                                    f" /{p.v.flt}" if p.v.flt else "",
                                    f" {human(p.free)} free" if p.free is not None and p.mode == "files" else "",
                                    " · 1 of 2, Tab" if s.single else ""))
        return f" {i + 1}  " + bar(where, right + " ", w - 7, "mid").rstrip() + " "  # host and folder end both stay

    def paint_pane(s, i, y, x, h, w, W):  # pane i in its rectangle: a title row, then its rows
        p, active, sc = s.panes[i], i == s.focus, s.scr
        v, n = p.v, h - 1
        v.top = min(max(v.top, v.sel - n + 1), v.sel)
        left = ("┌" if y == 1 else "├") if not x else "┬"
        right = ("┐" if y == 1 else "┤") if x + w == W else ""
        t = f"{left}─{s.title(i, p, w)}"
        sc.addstr(y, x, t + "─" * (w - cells(t) - len(right)) + right, s.C["acc" if active else "dim"])
        sc.addstr(y, x + cells(left) + 1, f" {i + 1} ", s.C["sel"] if active else s.C["dim"] | curses.A_REVERSE)  # a tab
        lx = x + bool(x)  # a pane on the right has the separator to its left
        cw = x + w - lx - 1
        for r in range(n):
            if x:
                sc.addstr(y + 1 + r, x, "│", s.C["dim"])
            j = r + v.top
            if j >= len(v.rows):
                continue
            if p.mode == "hosts":
                dep, k, hst, path = v.rows[j]
                ico = ("●" if hst is p.host else "○") if hst else ("▸" if path in p.closed else "▾")
                text, at = fit(f" {'  ' * dep}{ico} {k}", cw), s.C["norm" if hst else "dir"]
            else:
                name, a = v.rows[j]
                info = "" if folder(a) else human((a.get("dest") or a).get("size"))
                if cw >= 56 and a and "mtime" in a:  # room for the date too
                    info = f"{info:>7}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(a['mtime']))}"
                mark = "*" if name in p.marks else " "
                shown = name + (marker(a) if name != ".." else "") + (f" -> {a['target']}" if a and "target" in a else "")
                text = mark + fit(shown, cw - cells(info) - 2, "mid") + " " + info
                at = s.C["acc"] | curses.A_BOLD if mark == "*" else s.C[kind(name, a)]
            if j == v.sel and active:
                at = s.C["sel"]
            sc.addstr(y + 1 + r, lx, fit(text, cw), at)
        if v.top:
            sc.addstr(y + 1, lx + cw - 1, "▲", s.C["acc"])
        if v.top + n < len(v.rows):
            sc.addstr(y + n, lx + cw - 1, "▼", s.C["acc"])

    def paint_bar(s, y, items, W, key):  # a row of tappable labels: [(text, what)] -> spans remembered
        x, spans = 0, []
        s.scr.addstr(y, 0, " " * (W - 1), s.C["bar"])
        for text, what in items:
            if x + cells(text) >= W - 1:
                break
            lit = key == "menu" and what == s.opened  # the open menu stands out of the bar, in any theme, mono too
            s.scr.addstr(y, x, text, s.C["acc"] | curses.A_BOLD if lit else s.C["bar"] | curses.A_BOLD * (key == "menu"))
            spans.append((x, x + cells(text), what))
            x += cells(text) + 1
        s.spans[key] = spans

    def paint_log(s, y, ln, W):  # a log line: its time faint, ✓ and ✗ in their colours, an error's words too
        when, _, msg = ln.partition(" ")
        mark = msg[:1] if msg[:1] in "✓✗" else ""
        body = s.C["err"] if mark == "✗" else s.C["norm"]
        s.scr.addstr(y, 0, fit(f" {when} ", W - 1), s.C["dim"])
        x = cells(when) + 2
        if mark and x < W - 2:
            s.scr.addstr(y, x, mark, s.C["ok" if mark == "✓" else "err"] | curses.A_BOLD)
        if x + 2 < W - 1:
            s.scr.addstr(y, x + bool(mark) * 2, fit(msg[2:] if mark else msg, W - 1 - x - bool(mark) * 2), body)

    def paint_keys(s, y, W):  # mc's F-key bar: ten even slots, each the key's number, then its word on the bar
        keys = fkeys()
        slot, spans = max((W - 1) // len(keys), 1), []
        for j, (n, w, m) in enumerate(keys):
            x, num = j * slot, str(n)
            s.scr.addstr(y, x, num[:slot], s.C["norm"] | curses.A_BOLD)
            if slot > len(num):
                s.scr.addstr(y, x + len(num), fit(w, slot - len(num)), s.C["bar"])
            spans.append((x, x + slot, m))
        s.spans["fbar"] = spans

    def frame(s):
        sc = s.scr
        sc.erase()
        H, W = sc.getmaxyx()
        fit_ = layout(H, W)
        if fit_ and (len(fit_[0]) == 1) != s.single:
            s.single = len(fit_[0]) == 1
            if s.single:  # said once, and short: it shows when room is short
                s.log.append(f"{time.strftime('%H:%M:%S')} one pane: Tab swaps")
        if s.msg:  # every message lands in the log, which follows the newest
            s.log.append(f"{time.strftime('%H:%M:%S')} {s.msg}")
            s.msg, s.logoff = "", 0
        if not fit_:
            return sc.addstr(0, 0, fit(f"too small: {W}×{H}, needs {MIN_W}×{MIN_H}", W - 1))
        rects, logn = fit_
        s.rects = [(s.focus if i is None else i, *r) for i, *r in rects]
        A = H - 2 - logn  # the border's row
        with contextlib.suppress(curses.error):  # a cell off-screen mid-resize; the next frame fixes it
            s.paint_bar(0, [(f" {m} ", j) for j, m in enumerate(MENUS)], W, "menu")
            if s.spans["menu"] and s.spans["menu"][-1][1] + cells(ME) + 3 < W:  # where you are, at the right
                sc.addstr(0, W - 2 - cells(ME), ME, s.C["bar"])
            for r in s.rects:
                s.paint_pane(*r, W)
            c = s.P.cur() if s.P.mode == "files" else None
            status = lsl(*c) if c and c[1] else "activity"
            more = f" ↑{s.logoff} " if s.logoff else ""
            line = "└─ " + fit(status, W - 7 - cells(more), "end").rstrip() + " "
            text = cells(line)
            line += "─" * (W - 1 - text - cells(more)) + more + "┘"
            sc.addstr(A, 0, line, s.C["dim"])
            side = len(s.rects) == 2 and s.rects[1][2]  # side by side: where the separator meets the border
            if side and side >= text:
                sc.addstr(A, side, "┴", s.C["dim"])
            log = list(s.log)
            keep = logn - bool(s.working)  # the live line, when there is one, takes the last row
            shown = log[max(0, len(log) - keep - s.logoff):len(log) - s.logoff] if keep else []
            for k, ln in enumerate(shown):
                s.paint_log(A + 1 + k, ln, W)
            s.paint_work()
            if s.typing:
                hint = "⏎ apply  Esc clear" if s.typing == "/" else ""
                q = f" / {s.P.v.flt}" if s.typing == "/" else f" {s.typing}"
                sc.addstr(H - 1, 0, bar(q, hint, W), s.C["acc"] | curses.A_BOLD)
                sc.move(H - 1, min(cells(q), W - 2))
            else:
                s.paint_keys(H - 1, W)

    def act(s, fn, *a):  # run one action: errors are reported, the session stays
        t0 = time.monotonic()
        try:
            fn(*a)
        except KeyboardInterrupt:  # Ctrl-C mid-action cancels just the action
            s.msg = "✗ stopped"
        except Exception as e:
            s.msg = f"✗ {e}" if isinstance(e, OSError) else f"✗ {type(e).__name__}: {e}"
        finally:
            s.idle()
            if time.monotonic() - t0 > 0.5:
                curses.flushinp()  # keys hit while a long action ran shouldn't fire now

    def loop(s, start=()):  # start: host paths; one opens in the right pane, two in both
        if not start:
            s.act(s.open_host, s.panes[0], LOCAL)
        for i, path in enumerate(start, 2 - len(start)):
            s.focus = i
            s.pick(s.P, path)
            s.act(s.open)
        if len(start) == 1:
            s.act(s.open_host, s.panes[0], LOCAL)
        while True:
            s.paint()
            c = s.scr.getch()  # Ctrl-C here (idle) quits
            s.act(s.key, c)


def lcwd():  # the local folder; if someone removed it underneath us, home
    try:
        return os.getcwd()
    except OSError:
        os.chdir(os.path.expanduser("~"))
        return os.getcwd()
