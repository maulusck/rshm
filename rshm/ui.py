"""The screen, mc's way: a menu bar, two panes (each a folder here or on a host, or the host list), the activity log,
the F-key bar. Every action is one row of KEYS: keys, menu, F-key bar, F1 and --help all come from there."""
import atexit, contextlib, curses, fnmatch, grp, math, os, posixpath, pwd, shlex, shutil, signal, socket, stat
import subprocess, sys, tempfile, time
from collections import deque

from . import config, edit as editor, view as viewer
from .fs import (ARCHIVES, PART, Archive, Local, Remote, complete, copy, folder, isdir, islink, join, kind, look, marker,
                 names, pack, peek, rm, save, special, walk)
from .screen import (CLICK, MOVES, RIGHT, WHEEL_DOWN, WHEEL_UP, bar, cells, cursor, dur, fit, human, line, many, plain,
                     readline, safe, span, window)
from .ssh import argv, connect, quiet, unreachable

ROLES = ("norm", "sel", "acc", "dim", "err", "bar", "dir", "exe", "link", "stale", "archive", "media", "special",
         "hidden", "file", "ok")
THEMES = {  # each theme's colours, by name (xterm 256; -1 the terminal's own); every role of the screen derives from
    # them by one rule (pairs). sel: the cursor, bar: the menu and key bars, both (text, background)
    "default": dict(bg=-1, fg=-1, faint=-1, acc=6, err=1, ok=2, dir=4, exe=2, link=6, archive=5, media=3, special=3,
                    sel=(0, 6), bar=(0, 6)),  # your terminal's own colours
    "matrix": dict(bg=16, fg=34, faint=28, acc=46, err=196, ok=46, dir=46, exe=120, link=37, archive=112, media=77,
                   special=226, sel=(16, 46), bar=(46, 22)),  # phosphor
    "amber": dict(bg=16, fg=214, faint=130, acc=220, err=196, ok=228, dir=220, exe=228, link=179, archive=215, media=222,
                  special=231, sel=(16, 214), bar=(228, 94)),  # amber CRT
    "frutiger": dict(bg=195, fg=24, faint=102, acc=25, err=160, ok=22, dir=25, exe=22, link=23, archive=97, media=90,
                     special=94, sel=(17, 38), bar=(231, 25)),  # Aero: sky, aqua, glass
    "teletext": dict(bg=16, fg=231, faint=27, acc=51, err=196, ok=46, dir=226, exe=46, link=51, archive=201, media=46,
                     special=201, sel=(16, 226), bar=(226, 21)),  # Ceefax
    "avionics": dict(bg=16, fg=252, faint=242, acc=51, err=196, ok=46, dir=87, exe=46, link=80, archive=214, media=171,
                     special=220, sel=(16, 201), bar=(51, 236)),  # glass cockpit: cyan data, magenta selected, amber caution
    "robot": dict(bg=236, fg=252, faint=245, acc=208, err=210, ok=118, dir=214, exe=118, link=110, archive=180, media=175,
                  special=226, sel=(16, 220), bar=(16, 208)),  # machine shop: graphite, safety orange, warning yellow
    "cyberwave": dict(bg=53, fg=189, faint=103, acc=213, err=210, ok=121, dir=51, exe=121, link=87, archive=215, media=219,
                      special=226, sel=(16, 51), bar=(16, 213)),  # neon
    "steel": dict(bg=235, fg=146, faint=243, acc=110, err=174, ok=108, dir=153, exe=151, link=109, archive=144, media=182,
                  special=187, sel=(235, 110), bar=(254, 60)),  # blue-grey, calm
    "paper": dict(bg=230, fg=236, faint=138, acc=88, err=124, ok=22, dir=24, exe=22, link=23, archive=94, media=90,
                  special=130, sel=(236, 223), bar=(230, 94)),  # a book: cream page, ink, red rubrics, a highlighter
    "ember": dict(bg=16, fg=166, faint=130, acc=202, err=196, ok=178, dir=202, exe=208, link=173, archive=136, media=168,
                  special=220, sel=(16, 166), bar=(16, 172)),  # night: dark-adapted reds and oranges
}


def pairs(t):  # a theme -> (text, background[, b(old) d(im)]) per role, in ROLES order: one rule for every theme
    bg = t["bg"]
    faint = (t["faint"], bg, "d") if t["faint"] == t["fg"] else (t["faint"], bg)  # no faint colour: the dim attribute
    role = dict(norm=(t["fg"], bg), sel=t["sel"], acc=(t["acc"], bg, "b"), dim=faint, err=(t["err"], bg, "b"),
                bar=t["bar"], dir=(t["dir"], bg, "b"), exe=(t["exe"], bg, "b"), link=(t["link"], bg), stale=(t["err"], bg),
                archive=(t["archive"], bg), media=(t["media"], bg), special=(t["special"], bg, "b"), hidden=faint,
                file=(t["fg"], bg), ok=(t["ok"], bg, "b"))
    return [role[r] for r in ROLES]


MONO = dict(zip(ROLES, (0, curses.A_REVERSE, curses.A_BOLD, curses.A_DIM, curses.A_BOLD | curses.A_STANDOUT,
                        curses.A_REVERSE, curses.A_BOLD, curses.A_BOLD, 0, curses.A_DIM, 0, 0, curses.A_BOLD,
                        curses.A_DIM, 0, curses.A_BOLD)))
STYLE = {"b": curses.A_BOLD, "d": curses.A_DIM}
NAMES = {"Enter": (10, 13, curses.KEY_ENTER), "→": (curses.KEY_RIGHT,), "←": (curses.KEY_LEFT,), "Space": (32,),
         "Bksp": (curses.KEY_BACKSPACE, 127, 8), "Del": (curses.KEY_DC,), "Ins": (curses.KEY_IC,),
         "Tab": (9, curses.KEY_BTAB),
         "Esc": (27,), **{f"^{c}": (ord(c) - 64,) for c in "FLORU"}, **{f"F{i}": (curses.KEY_F0 + i,) for i in range(1, 11)}}
MENUS = ("Pane 1", "File", "Mark", "Command", "Options", "Pane 2")
KEYS = [  # (menu, keys, what, method): the one table; "Pane" rows show under both pane menus; no menu: help only
    ("File", "Enter → l", "open", "open"), ("File", "← h Bksp", "parent directory", "parent"),
    ("File", "i", "information", "info"), ("File", "F3 v", "view", "view"), ("File", "F4 e", "edit", "edit"),
    ("File", "F5 c", "copy to other pane", "copy"), ("File", "F6 m", "move to other pane", "move"),
    ("File", "F7 n", "mkdir", "mkdir"), ("File", "F8 x Del", "delete", "delete"), ("File", "R", "rename", "rename"),
    ("File", "A", "mode, owner, time", "chattr"), ("File", "L", "symbolic link (on a link: change target)", "symlink"),
    ("File", "H", "hard link", "hardlink"), ("File", "P", "pack into an archive", "pack"),
    ("File", "z", "disk usage", "size"),
    ("Mark", "Space Ins", "mark", "mark"), ("Mark", "*", "mark all / none", "markall"),
    ("Mark", "+", "mark by pattern", "markpat"), ("Mark", "-", "unmark by pattern", "unmarkpat"),
    ("Mark", "D", "mark differences", "compare"),
    ("Pane", "@", "host list", "hostlist"), ("Pane", "Tab", "other pane", "tab"), ("Pane", "/", "filter", "search"),
    ("Pane", "S", "sort", "sort"), ("Pane", ".", "show / hide dotfiles", "dotfiles"),
    ("Pane", "b", "history back", "back"), ("Pane", "f", "history forward", "forward"),
    ("Pane", "g", "go to directory", "goto"), ("Pane", "~", "home directory", "home"),
    ("Pane", "^F", "find by name", "find"), ("Pane", "o", "same directory in other pane", "mirror"),
    ("Pane", "^U", "swap panes", "swap"), ("Pane", "r ^R", "reread", "reload"), ("Pane", "Esc", "cancel", "esc"),
    ("Command", "F2", "menu", "menu"), ("Command", "^O s", "shell", "shell"), ("Command", "p", "check host", "ping"),
    ("Options", "F9", "pulldown menus", "menubar"), ("Options", "t", "theme", "theme"),
    ("Options", "[ ]", "scroll log", "logscroll"), ("Options", "", "edit config (F4 on host list)", "editcfg"),
    ("Options", "^L", "redraw", "draw"), ("Options", "F1 ?", "help", "help"), ("Options", "F10 q", "quit", "quit"),
    (None, "↑ ↓ j k PgUp PgDn Home End", "move cursor", None), (None, "click, click again", "select, open", None),
    (None, "right-click", "menu (F2)", None), (None, "wheel", "scroll", None),
]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
VERBS = {"copy": ("copying", "copied"), "move": ("moving", "moved"), "delete": ("deleting", "deleted"),  # each operation,
         "mkdir": ("creating", "created"), "rename": ("renaming", "renamed"), "link": ("linking", "linked"),  # as it runs
         "change": ("changing", "changed"), "save": ("saving", "saved"),  # and as the log
         "pack": ("packing", "packed")}  # keeps it
LONG = 0.5  # s: an action running longer shows itself (its log line, the box); keys typed meanwhile are dropped
HEAD = 1 << 20  # rshm's viewer shows a file's first HEAD bytes; its editor takes files up to HEAD
ORDER = {"name": lambda x: x[0].casefold(), "extension": lambda x: (posixpath.splitext(x[0])[1].casefold(), x[0]),
         "size": lambda x: -x[1].get("size", 0), "date": lambda x: -x[1].get("mtime", 0)}
LOCAL = {"host": "local"}  # the host-list entry for this machine
ME = f"{names(os.getuid(), os.getgid())[0]}@{socket.gethostname().split('.')[0]}"  # where you are, on the menu bar
SHELL = 'exec "${SHELL:-sh}" -l'
MIN_W, MIN_H, PANE_W, PANE_H = 20, 7, 24, 4  # the whole screen, and one pane (title row included), at the least


def grouped():  # KEYS by menu, as the bar orders them, the rest last: [(heading, [rows])]
    return [(m or "moving, mouse", [r for r in KEYS if r[0] == m]) for m in ("File", "Mark", "Pane", "Command", "Options", None)]


def keys_help():  # `rshm --help` shows the same table the app runs on, as the terminal shows it
    w = max(len(k) for _, k, _, _ in KEYS) + 2
    return plain("keys:" + "".join(f"\n {g}\n" + "\n".join(f"   {k:<{w}}{what}" for _, k, what, _ in rows)
                                   for g, rows in grouped()))


def codes(keys):  # "x Del" -> every keycode it stands for
    return [c for k in keys.split() for c in (NAMES[k] if k in NAMES else (ord(k),))]


def fkeys():  # [(n, label, method)]: what the F-keys do, mc's ten, from the table
    return sorted((int(k[1:]), w.split()[0].rstrip(":"), m) for _, keys, w, m in KEYS if m
                  for k in keys.split() if k[:1] == "F" and k[1:].isdigit())


def menu_rows(m):  # a menu's rows; the two pane menus share theirs
    return [r for r in KEYS if r[0] == ("Pane" if m.startswith("Pane") else m)]


def reason(e):  # an OSError as Unix tools put it, "path: Permission denied"; a part file named as the file it becomes
    names = " -> ".join(str(f) for f in (e.filename, e.filename2) if f is not None)
    return (f"{names}: {e.strerror}" if names else e.strerror or str(e)).replace(PART, "")


def entries(items):  # what an operation acts on, named: "notes.md", "build/", "3 entries"
    return items[0][0] + "/" * isdir(items[0][1]) if len(items) == 1 else f"{len(items)} entries"


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
    def __init__(s, scr, cfg, note="", extra=()):  # note: what the start had to say (a TERM drawn as another);
        s.cfg, s.scr, s.msg, s.typing = cfg, window(scr), "", None  # extra: destinations named at the start, in no list
        s.extra = config.tree({n: config.dest(n) for n in extra})
        s.panes, s.focus, s.rects, s.single, s.spans, s.opened = [Pane(), Pane()], 0, [], False, {}, None  # opened: menu
        s.local, s.remotes, s.hosts, s.c, s.cmds, s.theme_name, s.shown = Local(), {}, {}, {}, {}, "default", ""
        s.dots, s.order, s.rev = True, "name", False
        s.log, s.logoff, s.drawing = deque(maxlen=500), 0, False
        s.job, s.since, s.paused, s.held, s.ahead, s.doing = None, 0.0, False, 0.0, [], None  # what runs: (what, done
        # 0-1 or None, numbers), since when; held: seconds paused in all; ahead: keys typed while it ran, for after it;
        # doing: the operation under way, as its log line names it
        if note:
            s.note(note)
        cursor(0)
        curses.set_escdelay(25)
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        s.colours = curses.has_colors() and not os.environ.get("NO_COLOR")  # no-color.org: set, not empty: none
        s.defaults = False  # the terminal's own colours (-1), asked for always, or ncurses paints white on black
        if curses.has_colors():  # ponytail: curses.wrapper starts colours even under NO_COLOR, so a terminal without its
            curses.start_color()  # own (-1) shows white on black there; our own wrapper if one ever turns up
            with contextlib.suppress(curses.error):
                curses.use_default_colors()
                s.defaults = True
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
            raise OSError("no host selected")
        return s.P

    def load(s):  # the config (a broken one: what it had, at the start nothing) and ~/.ssh/config's hosts with it
        try:
            c = config.read(s.cfg)
            if config.unread(s.cfg):
                s.msg = config.unread(s.cfg)
        except OSError as e:
            c, s.msg = s.c, f"✗ config: {e}"
        old = {h["host"]: h for *_, h, _ in config.flat(s.hosts, set()) if h}
        s.c, s.hosts, s.cmds = c, {**config.hosts(c), **s.extra}, config.table(c, "commands")
        new = {h["host"]: h for *_, h, _ in config.flat(s.hosts, set()) if h}
        for k, h in new.items():  # a host keeps its last folder, a pane its host, through a reload
            if k in old and "_cwd" in old[k]:
                h["_cwd"] = old[k]["_cwd"]
        for p in s.panes:
            p.host = new.get(p.host["host"], p.host) if p.host and p.host is not LOCAL else p.host
        s.theme_name = str(c.get("theme", s.theme_name))
        s.set_theme(s.theme_name)
        for p in s.panes:
            s.sync(p)

    def can(s, name):  # can this terminal show the theme: as many colours, and its own (-1) where the theme uses them?
        t = pairs(THEMES[name])
        fits = max(max(x[:2]) for x in t) < curses.COLORS
        return s.colours and fits and (s.defaults or min(min(x[:2]) for x in t) >= 0)

    def lack(s):  # why a theme can't show
        return "NO_COLOR set" if curses.has_colors() and not s.colours else \
            f"terminal has {curses.COLORS if s.colours else 'no'} colours"

    def set_theme(s, name):  # the theme as named (and saved); shown if this terminal can, else the default theme, else
        s.theme_name = name if name in THEMES else "default"  # none: bold, reverse and dim. Never a theme half shown
        shown = next((n for n in (s.theme_name, "default") if s.can(n)), None)
        s.C = MONO
        if shown:
            t = pairs(THEMES[shown])
            for i, x in enumerate(t, 1):
                curses.init_pair(i, x[0], x[1])
            s.C = {r: curses.color_pair(i) | STYLE.get(x[2:3] and x[2], 0) for i, (r, x) in enumerate(zip(ROLES, t), 1)}
        if shown not in (s.theme_name, s.shown):  # a theme not shown as named: said once
            s.note(f"theme {s.theme_name}: {s.lack()}, using {shown or 'no colours'}")
        s.shown = shown
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
    @staticmethod
    def where(p):  # a pane's folder, as titles and the log name it
        return f"{p.fs.name}:{p.cwd}"

    def op(s, verb, what, fn):  # one operation, one log line: "copying x to y" while it runs, then "✓ copied x to y"
        ing, ed = VERBS[verb]     # and fn's own words (its numbers), or "✗ copying x to y: why"
        s.doing = f"{ing} {what}"
        try:
            more = fn() or ""
        except OSError as e:
            s.doing = None
            raise OSError(f"{ing} {what}: {reason(e)}") from None
        s.doing = None
        s.note(f"✓ {ed} {what}{more}")

    def busy(s, what, done=None, numbers=""):  # an action says how far it is: its keys read; shown once it runs long
        if not s.job:
            s.since = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, 0.12, 0.12)  # the tick: the same, while a call blocks
        s.job = what, done, numbers
        s.spin()

    def spin(s, *_):  # SIGALRM: Python runs this between two steps, never inside a curses call; never mid-frame
        if s.job and not s.drawing:
            if s.long():
                s.paint()
            s.poll()

    def turn(s):  # what runs, in one cell: turning, or paused
        return "‖" if s.paused else SPIN[int(time.monotonic() * 8) % len(SPIN)]

    def long(s):  # an action shows itself once it runs long, or at once when paused
        return s.paused or time.monotonic() - s.since >= LONG

    def poll(s):  # the keys hit meanwhile, all of them: Esc stops (as ^C does), Space pauses; any other, for after
        s.scr.refresh()
        s.scr.nodelay(True)
        try:
            ks = [s.button(k) for k in iter(s.scr.getch, -1)]
        finally:
            s.scr.nodelay(False)
        s.ahead += [k for k in ks if k not in (27, 32, curses.KEY_MOUSE, curses.KEY_RESIZE)]
        if 27 in ks:
            raise KeyboardInterrupt
        if 32 in ks:
            s.pause()

    def pause(s):  # nothing moves until Space again, Esc stops; the clock stands still (a pause isn't a slow link)
        signal.setitimer(signal.ITIMER_REAL, 0)
        s.paused, t, k = True, time.monotonic(), None
        try:
            while k not in (32, 27):
                s.paint()
                k = s.button(s.scr.getch())
        finally:
            s.paused, s.held = False, s.held + time.monotonic() - t
            signal.setitimer(signal.ITIMER_REAL, 0.12, 0.12)
        if k == 27:
            raise KeyboardInterrupt
        s.paint()
        s.scr.refresh()

    def clock(s):  # seconds, paused time left out: what speeds and times are measured by
        return time.monotonic() - s.held

    def button(s, k):  # a tap on a button of the box is its key; any other tap, nothing
        with contextlib.suppress(curses.error):
            if k == curses.KEY_MOUSE:
                _, x, y, _, b = curses.getmouse()
                k = next((key for y0, x0, x1, key in s.spans.get("job", ()) if y == y0 and x0 <= x < x1 and b & CLICK), k)
        return k

    def idle(s):
        signal.setitimer(signal.ITIMER_REAL, 0)
        s.job = None

    @contextlib.contextmanager
    def still(s):  # no spinner while someone types: prompts, or the terminal handed to ssh/an editor
        signal.setitimer(signal.ITIMER_REAL, 0)
        try:
            yield
        finally:
            if s.job:
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

    def ticker(s, label, total):  # progress: how far, how fast, how long still
        last, seen, rate, pos = s.clock(), 0, None, {}

        def tick(path, at, resumed=False):  # resumed: bytes found already there, not moved now
            nonlocal last, seen, rate
            step, pos[path] = at - pos.get(path, 0), at
            tick.done += step
            tick.moved += 0 if resumed else step
            now = s.clock()
            if now - last > 0.25:  # speed: a moving average over ~5 s of time (chunks come unevenly), so the time
                got, w = (tick.moved - seen) / (now - last), 1 - math.exp((last - now) / 5)  # left follows the link
                rate, last, seen = got if rate is None else rate + w * (got - rate), now, tick.moved
                eta = f"   {dur((total - tick.done) / rate)} left" if total and rate else ""  # nothing moved: no guess
                files = f"   {many(len(tick.finished), 'file')}" if tick.finished else ""
                s.busy(label, tick.done / total if total else None,
                       f"{human(tick.done)}{f' of {human(total)}' if total else ''}   {human(rate)}/s{eta}{files}")
        tick.done, tick.moved, tick.finished, tick.skipped = 0, 0, set(), []
        return tick

    def xfer(s, fn, label, total, fss):  # fn(tick) over file systems fss -> its numbers; a lost link reconnects, resumes
        tick, fails, seen, t0 = s.ticker(label, total), 0, 0, s.clock()
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
            s.msg = f"✗ {label}: connection lost ({err})"
            s.busy(label, None, f"connection lost, retrying in {min(2 ** fails, 20)}s")
            time.sleep(min(2 ** fails, 20))
        n, took = f", {many(len(tick.finished), 'file')}" if len(tick.finished) > 1 else "", s.clock() - t0
        speed = f", {human(tick.moved / took)}/s" if took >= 1 else ""
        return f"  {human(tick.done)}{n} in {dur(took)}{speed}"

    # --- moving around ---
    def ls(s, p, d, keep=None, record=True):  # list folder d in pane p; keep: a name for the cursor; record: history
        s.busy(f"listing {safe(d) or '~'}")  # a path from / by name, as cd takes it: .. out of a link is back where
        d = posixpath.normpath("/" + d.lstrip("/")) if d[:1] == "/" else p.fs.realpath(d)  # it was, not its target
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
            s.xfer(lambda t: copy(p.fs, join(p.cwd, name), s.local, lp, t, follow=True), f"fetching {name}",
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
            raise OSError("end of history")
        h, d = src.pop()
        dst.append(p.here())
        s.open_host(p, h, d, record=False) if h is not p.host or p.up else s.ls(p, d, record=False)

    def path(s, p, x):  # a typed path: as is from / or ~, else from the pane's folder
        return x if x[:1] in ("/", "~") else join(p.cwd, x)

    def goto(s):
        p = s.folder()
        d = s.ask("go to:", p.cwd, p.fs, p.cwd)
        if d:
            s.ls(p, s.path(p, d))

    def home(s):
        s.ls(s.folder(), "")

    def mirror(s):  # the other pane to this folder
        p = s.folder()
        if p.up:
            raise OSError("not supported in an archive")
        s.open_host(s.O, p.host, p.cwd)

    def swap(s):
        s.panes.reverse()
        s.focus ^= 1

    def tab(s):
        s.focus ^= 1

    def find(s):  # ^F: names like a pattern, anywhere below this folder; the results become the pane's listing
        p = s.folder()
        pat = s.ask("find pattern:", "*")
        if not pat:
            return
        found = []
        try:
            for rel, a in walk(p.fs, p.cwd):
                if fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pat):
                    found.append((rel, a))
                    if len(found) % 50 == 0:
                        s.busy(f"find {pat}", None, f"{len(found)} found")
        except KeyboardInterrupt:
            s.msg = f"✗ find {pat}: stopped, {len(found)} found"
        p.items, p.found, p.marks, p.files.sel, p.files.top = [("..", None), *found], pat, set(), 0, 0
        s.sync(p)
        s.msg = s.msg or f"✓ find {pat}: {len(found)} found"

    def search(s):  # filter this view: type to narrow, arrows move; ⏎ applies (never opens), Esc clears
        p = s.P
        v = p.v
        s.typing, v.flt = "/", ""
        cursor(1)
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
            cursor(0)

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
        pat = s.ask(f"{'mark' if plus else 'unmark'} pattern:", "*")
        hit = {n for n, a in p.files.rows if a and fnmatch.fnmatchcase(n, pat)} if pat else set()
        p.marks = p.marks | hit if plus else p.marks - hit

    def unmarkpat(s):
        s.markpat(False)

    def compare(s):  # D: marks what's missing in the other pane, or (files) differs in size or is newer here
        p, o = s.folder(), s.O
        if o.mode != "files":
            raise OSError("other pane: no directory")
        p.marks = differ(p.items, o.items)
        s.msg = f"{len(p.marks)} differ, marked" if p.marks else "✓ no differences"

    # --- files ---
    def info(s):  # i: everything about the entry under the cursor, in the viewer (where a long name wraps whole)
        p = s.folder()
        c = p.cur()
        if not c or not c[1]:
            return
        n, a = c
        when = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        rows = [("name", n), ("in", s.where(p)), ("kind", kind(n, a)),
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
            raise OSError(f"{name}: not a regular file")
        cmd = config.opener(s.c, name) if how == "open" else None
        if cmd:
            return s.external(p, name, a, cmd, False)
        if how == "open" and name.lower().endswith(ARCHIVES):
            return s.enter(p, name, a)
        size = p.fs.stat(path).get("size", 0)  # as it is now, not as listed
        tool = config.tool(s.c, "edit" if how == "edit" else "view")
        if how == "edit" and not tool and size > HEAD:
            raise OSError(f"{name}: too large to edit ({human(size)}, limit {human(HEAD)}; see [tools] edit)")
        data = s.contents(p, path, min(size, 8192 if tool else HEAD))  # enough to tell text from binary, or all it shows
        if b"\0" in data[:8192]:  # binary: never to an editor, never to a text tool
            if how == "edit":
                raise OSError(f"{name}: binary file")
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
                editor.edit(s.scr, data, title, s.C, lambda b: s.op("save", f"{name} in {s.where(p)}",
                                                                    lambda: save(p.fs, path, b)))
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
                def send():
                    nonlocal keep
                    try:
                        return s.xfer(lambda t: copy(s.local, lp, p.fs, path, t), s.doing, os.path.getsize(lp), [p.fs])
                    except BaseException as e:
                        keep = True
                        why = reason(e) if isinstance(e, OSError) else "stopped"
                        raise OSError(f"{why}; edit kept in {lp}") from None
                s.op("save", f"{name} in {s.where(p)}", send)
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
            raise OSError("other pane: no directory")
        if move and isinstance(p.fs, Archive):
            raise OSError("read-only archive")
        what = entries(items)
        dst = s.ask(f"{'move' if move else 'copy'} {what} to {o.fs.name}:", o.cwd, o.fs, o.cwd)
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
            raise OSError(f"{dst}: not a directory")
        same = o.fs is p.fs
        if same:  # by real paths: "a/../b" or a link into the tree mustn't get past these
            real = lambda t: join(o.fs.realpath(posixpath.dirname(t)), posixpath.basename(t))
            src = {n: p.fs.realpath(join(p.cwd, n)) for n, _, _ in pairs}
            pairs = [x for x in pairs if real(x[2]) != src[x[0]]]  # onto itself: nothing to do
            if any(real(t).startswith(src[n] + "/") for n, _, t in pairs):
                raise OSError("cannot copy a directory into itself")
        if not pairs:
            raise OSError("source and destination are the same")
        if len({t for *_, t in pairs}) < len(pairs):  # find results from different folders, one name
            raise OSError("duplicate names in selection")
        clash = {n for n, _, t in pairs if t.rsplit("/", 1)[-1] in there}
        more = f" and {len(clash) - 1} more" if len(clash) > 1 else ""
        if clash and not s.confirm(f"overwrite {min(clash)}{more} (directories merge)? (y/N)"):
            return
        kept, names = [], {n for n, _, _ in pairs}  # kept: moved trees holding pipes, sockets or devices stay

        def body():
            if move and same:  # one file system: a rename, nothing travels; only a folder merge needs copies
                for n, a, t in [x for x in pairs if not (x[0] in clash and isdir(x[1]))]:
                    s.busy(s.doing, None, n)
                    p.fs.rename(join(p.cwd, n), t)
                    pairs.remove((n, a, t))
            if not pairs:
                return ""
            total = s.measure(p.fs, p.cwd, [(n, a) for n, a, _ in pairs])[0]  # ponytail: one listing pass first, for
            # a whole to measure progress and time left against; incremental counting if huge trees make it wait
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
            return s.xfer(run, s.doing, total, [p.fs, o.fs]) + \
                (f"; {', '.join(kept)} kept (contains pipes, sockets or devices)" if kept else "")
        try:
            s.op("move" if move else "copy", f"{what} to {o.fs.name}:{dst}", body)
        finally:
            p.marks -= names
            s.relist()

    def mkdir(s):
        p = s.folder()
        name = s.ask("mkdir:", "", p.fs, p.cwd).strip("/")
        if name:
            s.op("mkdir", f"{name}/ in {s.where(p)}", lambda: p.fs.mkdir(join(p.cwd, name)))
            s.ls(p, p.cwd, keep=name.split("/")[0])

    def delete(s):
        p = s.folder()
        items = p.targets()
        what = entries(items)
        if items and s.confirm(f"delete {what}? (y/N)"):
            def body():
                for n, _ in items:
                    s.busy(s.doing, None, n)
                    rm(p.fs, join(p.cwd, n))
                    p.marks.discard(n)
            try:
                s.op("delete", f"{what} in {s.where(p)}", body)
            finally:
                s.relist()  # show what's left even if it stopped halfway

    def rename(s):  # never overwrites; "sub/", "sub/x" or "/abs/x" move it on this file system, like mv
        p = s.folder()
        c = p.cur()
        if not c or not c[1]:
            return
        new = s.ask("rename to:", c[0], p.fs, p.cwd)
        if new and new != c[0]:
            dst = new if new.startswith("/") else join(p.cwd, new)
            dst = join(dst, c[0]) if new.endswith("/") else dst
            s.op("rename", f"{c[0]} to {new} in {s.where(p)}",
                 lambda: p.fs.rename(join(p.cwd, c[0]), dst, replace=False))
            s.ls(p, p.cwd, keep=new)

    def symlink(s):  # L: a symlink to the entry under the cursor, where you say; on a link: where it points, changed
        p = s.folder()
        c = p.cur()
        if c and c[1] and islink(c[1]):
            path = join(p.cwd, c[0])
            to = s.ask("link target:", c[1]["target"], p.fs, p.cwd)
            if to and to != c[1]["target"]:
                def swap():  # the new link beside the old, then over it: never neither
                    p.fs.symlink(to, path + PART)
                    p.fs.rename(path + PART, path)
                s.op("link", f"{c[0]} -> {to} in {s.where(p)}", swap)
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
        o, name, src = s.O, posixpath.basename(c[0]), join(p.fs.realpath(p.cwd), c[0])  # real: what relpath needs
        here = o.cwd if o.mode == "files" and o.fs is p.fs and not o.found else p.cwd  # one file system only
        at = s.ask(f"{kind} {name} as:", join(here, name), p.fs, p.cwd)
        if not at:
            return
        at = s.path(p, at)
        at = join(p.fs.realpath(posixpath.dirname(at)), posixpath.basename(at))
        d, new = posixpath.split(at)
        if kind == "symlink":  # relative: it still points right when the tree moves or is mounted elsewhere
            to = posixpath.relpath(src, d)
            s.op("link", f"{new} -> {to} in {p.fs.name}:{d}", lambda: p.fs.symlink(to, at))
        else:
            s.op("link", f"{new} to {src} in {p.fs.name}:{d}", lambda: p.fs.hardlink(src, at))
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
            raise OSError(f"{name}: no such {what}")
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
                    raise OSError(f"invalid {label}: {v.strip()} (as in {shown})") from None
                done.append(f"{label} {v.strip()}")
        if ch:
            def apply():
                for n, _ in items:
                    p.fs.chattr(join(p.cwd, n), ch)
            s.op("change", f"{entries(items)} in {s.where(p)}: {', '.join(done)}", apply)
            s.relist()

    def pack(s):  # P: the marked entries (or the one under the cursor) into a new archive in the other pane's folder,
        p, o = s.folder(), s.O  # its kind by the name you give it; from anywhere to anywhere, as a copy goes
        items = p.targets()
        if not items:
            return
        if o.mode != "files" or isinstance(o.fs, Archive):
            raise OSError("other pane: no directory" if o.mode != "files" else "read-only archive")
        what = entries(items)
        base = posixpath.basename(items[0][0] if len(items) == 1 else p.cwd) or "archive"
        at = s.ask(f"pack {what} as:", join(o.cwd, f"{base}.tar.gz"), o.fs, o.cwd)
        if not at:
            return
        at = s.path(o, at)
        at = join(o.fs.realpath(posixpath.dirname(at)), posixpath.basename(at))
        if not at.lower().endswith(ARCHIVES):
            raise OSError(f"{posixpath.basename(at)}: name it {', '.join(ARCHIVES)}")
        old = peek(o.fs, at)
        if old and isdir(old):
            raise OSError(f"{at}: is a directory")
        if old and not s.confirm(f"overwrite {posixpath.basename(at)}? (y/N)"):
            return
        skipped = []

        def body():  # built here (on this machine, as NAME.rshm-part), then into place; a host's copy goes over after
            total = s.measure(p.fs, p.cwd, items)[0]
            d = None if isinstance(o.fs, Local) else tempfile.mkdtemp()
            out = at + PART if d is None else os.path.join(d, posixpath.basename(at))
            if d is None:  # made by its file system (what may write where), then filled
                o.fs.write(out, iter(()), 0, {"perm": 0o644}, None)

            def run(tick):  # ponytail: a dropped link starts the archive over; resume needs an appendable format
                tick.skipped.clear()
                pack(p.fs, p.cwd, items, out, tick)
                skipped[:] = tick.skipped
            try:
                more = s.xfer(run, s.doing, total, [p.fs])
                if d is None:
                    o.fs.rename(out, at)
                else:
                    more = s.xfer(lambda t: copy(s.local, out, o.fs, at, t), s.doing, os.path.getsize(out), [o.fs])
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(out)
                d and shutil.rmtree(d, ignore_errors=True)
            return more + (f"; {many(len(skipped), 'pipe, socket or device')} left out" if skipped else "")
        try:
            s.op("pack", f"{what} into {o.fs.name}:{at}", body)
        finally:
            p.marks -= {n for n, _ in items}
            s.relist()

    def size(s):  # z: the size of the marked entries, or of the one under the cursor (a folder: all inside it)
        p = s.folder()
        items = p.targets()
        total, files = s.measure(p.fs, p.cwd, items)
        if items:
            s.msg = f"{items[0][0] if len(items) == 1 else many(len(items), 'item')}: {human(total)} in {many(files, 'file')}"

    def measure(s, fs, d, items):  # (bytes, files) of the files in items (in folder d), trees counted all through:
        total = files = 0               # what a copy of them moves; Esc stops the count
        for n, a in items:
            for i, (_, b) in enumerate(walk(fs, join(d, n)) if isdir(a) else [(n, a)]):
                if stat.S_ISREG(b.get("perm", 0)):
                    total, files = total + b.get("size", 0), files + 1
                if i % 200 == 0:
                    s.busy(s.doing or f"counting {n}", None, f"{human(total)} in {many(files, 'file')} so far")
        return total, files

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
        s.run(p.fs, f"{cmd}; printf '\\n[exit %s] Enter: back ' $?; read _", p.cwd)

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
            raise OSError(f"{h['host']}: unreachable ({e})" if unreachable(str(e)) else
                          f"{h['host']}: up, login required") from None
        s.busy(f"checking {h['host']}")
        t0 = time.monotonic()
        rc, err = quiet([*argv(h, batch=True, timeout=3), h["host"], "true"])
        if rc:
            raise OSError(f"{h['host']}: {err}")
        s.msg = f"✓ {h['host']}: up ({(time.monotonic() - t0) * 1000:.0f} ms)"

    # --- options ---
    def theme(s):  # t: the themes this terminal can show; the pick is saved (no config: this session only)
        names = [n for n in THEMES if s.can(n)]
        if not names:
            raise OSError(f"no themes: {s.lack()}")
        i = s.choose([(n, "✓" if n == s.shown else "") for n in names], title="theme")
        if isinstance(i, int):
            s.set_theme(names[i])
            if s.cfg.exists():
                config.save(s.cfg, config.retheme(s.cfg.read_text(), s.theme_name))
            s.msg = f"theme: {s.theme_name}"

    def editcfg(s):  # the config (from the sample, the first time) in your editor or rshm's own
        config.create(s.cfg)
        cmd = config.tool(s.c, "edit")
        if cmd:
            s.term(["sh", "-c", config.cmdline(cmd, str(s.cfg))])
        else:
            with s.still():
                editor.edit(s.scr, s.cfg.read_bytes(), str(s.cfg), s.C, lambda b: s.op(
                    "save", f"{s.cfg.name} in local:{s.cfg.parent}", lambda: save(s.local, str(s.cfg), b)))
        s.load()

    def logscroll(s, d=None):  # [ older, ] newer (and the wheel over the log)
        d = d if d is not None else -1 if s.key_hit == ord("]") else 1
        s.logoff = max(0, min(len(s.log) - 1, s.logoff + d))

    def help(s, y=None, x=None):  # every key, by menu; ⏎ runs the chosen one
        rows = [r for g, rs in grouped() for r in [(None, None, g, None), *rs]]
        i = s.choose([(w, k) for _, k, w, _ in rows], y, x, title="keys")
        if isinstance(i, int) and rows[i][3]:
            s.act(getattr(s, rows[i][3]))

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

    def quit(s):  # asked once; the same key again (q q, F10 F10) says yes
        if s.confirm("quit? (y/N)", (ord("q"), curses.KEY_F10)):
            sys.exit()

    # --- prompts ---
    def ask(s, q, text="", fs=None, base=""):  # the one-line input at the bottom, over the current screen; a path's:
        with s.still():                             # fs, base: Tab completes it there
            return (readline(s.scr, q, text, s.C["acc"] | curses.A_BOLD, s.paint,
                             fs and (lambda t: complete(fs, base, t))) or "").strip()

    def confirm(s, q, yes=()):  # one key: y (or one of yes) says yes, anything else no
        s.typing = q
        try:
            with s.still():
                s.paint()
                return s.scr.getch() in (ord("y"), ord("Y"), *yes)
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
        w = min(max(max(cells(l) + cells(k or "") for l, k in items) + 6, cells(title) + 6), W)
        h = min(len(items) + 2, H)
        y, x = max(0, min(y0, H - h)), max(0, min(x0, W - w))
        win = window(curses.newwin(h, w, y, x))
        win.keypad(True)
        win.bkgd(" ", s.C["norm"])
        off = max(0, i - h + 3)  # scroll when the terminal is shorter than the menu
        with contextlib.suppress(curses.error):  # a terminal too small for a frame: what fits shows
            top = f"─ {title} " if title else ""
            win.addstr(0, 0, "┌" + top + "─" * (w - 2 - cells(top)) + "┐", s.C["acc"])
            if title:
                win.addstr(0, 2, f" {title} ", s.C["acc"] | curses.A_BOLD)
            for r, (l, k) in enumerate(items[off:off + h - 2]):
                if k is None:  # a heading: a divider through the frame, with its name
                    win.addstr(r + 1, 0, "├" + span(f"─ {l} " + "─" * w, 0, w - 2) + "┤",
                               s.C["acc"] | curses.A_REVERSE * (r + off == i))
                else:
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
                                  lambda: s.popup(items, i, y0, x0, title), (curses.KEY_UP, curses.KEY_DOWN, "\t", curses.KEY_BTAB))
            if k in ("\n", "\x1b"):
                return vals if k == "\n" else None
            i = (i + (-1 if k in (curses.KEY_UP, curses.KEY_BTAB) else 1)) % len(rows)

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
        s.drawing, s.spans["job"] = True, []
        try:
            s.frame()
            if s.job and s.long():
                s.paint_job()
        finally:
            s.drawing = False

    def paint_job(s):  # the box over the screen: what runs, centred on top; its bar (its time, while the size is unknown)
        what, done, numbers = s.job  # and numbers; under a rule, its two buttons
        H, W = s.scr.getmaxyx()
        w = max(min(64, W - 4), 16)
        buttons = [(f"[ Space {'resume' if s.paused else 'pause'} ]", 32), ("[ Esc stop ]", 27)]
        if sum(cells(b) + 3 for b, _ in buttons) > w - 2:  # a narrow screen: the keys alone
            buttons = [("[ Space ]", 32), ("[ Esc ]", 27)]
        rows = 1 + bool(numbers)
        y, x = max((H - rows - 4) // 2, 0), max((W - w) // 2, 0)
        by, bx = y + rows + 2, x + (w - sum(cells(b) + 3 for b, _ in buttons) + 3) // 2
        A, N = s.C["acc"], s.C["norm"]
        with contextlib.suppress(curses.error):  # a terminal too small for the box: what fits shows
            title = f" {fit(what, w - 6).rstrip()} "
            left = (w - 2 - cells(title)) // 2
            s.scr.addstr(y, x, f"┌{'─' * left}{title}{'─' * (w - 2 - left - cells(title))}┐", A | curses.A_BOLD)
            for r in range(1, rows + 3):
                s.scr.addstr(y + r, x, f"├{'─' * (w - 2)}┤" if r == rows + 1 else f"│{' ' * (w - 2)}│", A)
            s.scr.addstr(y + 1, x + 2, s.turn(), A | curses.A_BOLD)
            if done is None:
                s.scr.addstr(y + 1, x + 4, dur(time.monotonic() - s.since), N)
            else:
                n = w - 11
                full = round(done * n)
                s.scr.addstr(y + 1, x + 4, "█" * full, A)
                s.scr.addstr(y + 1, x + 4 + full, "░" * (n - full), s.C["dim"])
                s.scr.addstr(y + 1, x + 4 + n, f" {done:4.0%}", N)
            if numbers:
                s.scr.addstr(y + 2, x + 4, fit(numbers, w - 6), N)
            for b, k in buttons:
                s.scr.addstr(by, bx, b, s.C["sel"])
                s.spans["job"].append((by, bx, bx + cells(b), k))
                bx += cells(b) + 3
            s.scr.addstr(by + 1, x, f"└{'─' * (w - 2)}┘", A)

    def title(s, i, p, w):  # which pane, and what it shows: its host and folder, the host list, an archive, a find
        where = "hosts" if p.mode == "hosts" else f"find {p.found} in {s.where(p)}" if p.found \
            else s.where(p)
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

    def note(s, text):  # a line in the log, timed; the log follows it
        s.log.append(f"{time.strftime('%H:%M:%S')} {text}")
        s.logoff = 0

    def frame(s):
        sc = s.scr
        sc.erase()
        H, W = sc.getmaxyx()
        fit_ = layout(H, W)
        if fit_ and (len(fit_[0]) == 1) != s.single:
            s.single = len(fit_[0]) == 1
            if s.single:  # said once, and short: it shows when room is short
                s.note("one pane: Tab swaps")
        if s.msg:  # every message lands in the log, which follows the newest
            s.note(s.msg)
            s.msg = ""
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
            log, live = list(s.log), bool(logn and s.job and s.long())  # live: what runs, as the log's newest line
            keep = logn - live
            shown = log[max(0, len(log) - keep - s.logoff):len(log) - s.logoff] if keep else []
            for k, ln in enumerate(shown):
                s.paint_log(A + 1 + k, ln, W)
            if live:
                what, _, numbers = s.job
                s.paint_log(H - 2, f"{time.strftime('%H:%M:%S')} {s.turn()} {what}{f'  {numbers}' * bool(numbers)}", W)
            if s.typing:
                hint = "Enter apply  Esc clear" if s.typing == "/" else ""
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
            what = s.doing or s.job and s.job[0]
            s.msg = f"✗ {what}: stopped" if what else "✗ stopped"
        except Exception as e:
            s.msg = f"✗ {reason(e)}" if isinstance(e, OSError) else f"✗ {type(e).__name__}: {e}"
        finally:
            s.idle()
            s.doing = None
            ahead, s.ahead = s.ahead, []
            if time.monotonic() - t0 > LONG:
                curses.flushinp()  # keys hit while a long action ran shouldn't fire now; after a short one, they do
            else:
                for k in reversed(ahead):
                    curses.ungetch(k)

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


def lcwd():  # the local folder, by the name the shell gave it ($PWD: through links); if someone removed it, home
    try:
        here = os.getcwd()
    except OSError:
        os.chdir(os.path.expanduser("~"))
        return os.getcwd()
    named = os.environ.get("PWD", "")
    with contextlib.suppress(OSError):
        if named[:1] == "/" and os.path.samefile(named, here):
            return named
    return here
