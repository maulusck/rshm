"""The screen: two panes, each showing a folder on this machine or on any host, or the host list to pick one from."""
import atexit, contextlib, curses, fnmatch, os, shlex, shutil, signal, stat, subprocess, sys, tempfile, time, tomllib
from collections import deque

from . import config, edit as editor, screen, view as viewer
from .fs import ARCHIVES, Archive, Local, Remote, copy, isdir, islink, join, peek, rm, save
from .screen import MOVES, human, readline, safe
from .ssh import argv, connect, quiet, unreachable

THEMES = {  # (fg, bg) for norm, sel, acc, dim, err, brand. -1 = terminal default
    "default":  [(-1, -1), (0, 6), (6, -1), (8, -1), (1, -1), (0, 6)],
    "matrix":   [(46, -1), (0, 46), (82, -1), (28, -1), (196, -1), (0, 46)],
    "amber":    [(214, -1), (0, 214), (220, -1), (130, -1), (196, -1), (0, 214)],
    "frutiger": [(117, -1), (232, 122), (80, -1), (67, -1), (204, -1), (232, 75)],
    "teletext": [(7, -1), (0, 3), (6, -1), (4, -1), (1, -1), (0, 3)],
}
MONO = [0, curses.A_REVERSE, curses.A_BOLD, curses.A_DIM, curses.A_STANDOUT, curses.A_REVERSE]
WHEEL_UP, WHEEL_DOWN = curses.BUTTON4_PRESSED, getattr(curses, "BUTTON5_PRESSED", 0x200000)
LEFT_CLICK, RIGHT_CLICK = curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED, curses.BUTTON3_PRESSED | curses.BUTTON3_CLICKED
NAMES = {"⏎": (10, 13, curses.KEY_ENTER), "→": (curses.KEY_RIGHT,), "←": (curses.KEY_LEFT,), "Space": (32,),
         "⌫": (curses.KEY_BACKSPACE, 127, 8), "Del": (curses.KEY_DC,), "Ins": (curses.KEY_IC,), "Tab": (9,),
         "Esc": (27,), "^O": (15,), "^R": (18,), **{f"F{i}": (curses.KEY_F0 + i,) for i in range(1, 11)}}
KEYS = {  # the one source for key dispatch, the help menu, the F-key bar and --help: (keys, what, method)
    "files": [("⏎ → l", "open / view", "open"), ("← h ⌫", "parent folder", "parent"),
              ("Space Ins", "mark", "mark"), ("*", "mark all / none", "markall"),
              ("+ -", "mark / unmark by pattern", "markpat"), (".", "dotfiles: show / hide", "dotfiles"),
              ("S", "sort: name, size, date", "sort"), ("A", "permissions", "chmod"),
              ("F3 v", "view", "view"), ("F4 e", "edit", "edit"),
              ("F5 c", "copy to the other pane", "copy"), ("F6 m", "move to the other pane", "move"),
              ("F7 n", "new folder", "mkdir"), ("F8 x Del", "delete", "delete"), ("R", "rename", "rename"),
              ("F2", "menu of your [commands]", "menu"), ("^O s", "shell here", "shell"),
              ("g", "go to path", "goto"), ("~", "home", "home")],
    "hosts": [("⏎ → l Space", "open host / fold group", "hopen"), ("← h ⌫", "fold / to group", "hclose"),
              ("^O s", "shell on host", "hshell"), ("e", "edit config", "editcfg")],
    "both": [("↑ ↓ j k PgUp PgDn Home End", "move", None), ("Tab", "other pane", "tab"),
             ("F9 @", "hosts / back", "hostlist"), ("/", "filter: ⏎ applies, Esc clears", "search"),
             ("Esc", "clear filter / marks, back", "esc"), ("p", "live check", "ping"), ("t", "next theme", "cycle_theme"),
             ("r ^R", "reload", "reload"), ("F1 ?", "help: all keys", "help"), ("F10 q", "quit", "quit"),
             ("click", "select; again: open", None), ("right-click", "help", None), ("wheel", "scroll", None)],
}
SPIN, BIG = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 50 << 20  # spinner frames; view/edit asks before loading more than BIG bytes
ORDER = {"name": lambda x: x[0].casefold(), "size": lambda x: -x[1].get("size", 0), "date": lambda x: -x[1].get("mtime", 0)}
LOCAL = {"host": "local"}  # the host-list entry for this machine
SHELL = 'exec "${SHELL:-sh}" -l'


def keys_help():  # `rshm --help` shows the same table the app runs on
    return "\n".join(f"{title}:\n" + "\n".join(f"  {keys:<26} {what}" for keys, what, _ in KEYS[v])
                     for v, title in (("files", "files"), ("hosts", "host list"), ("both", "everywhere")))


def codes(keys):  # "x Del" -> every keycode it stands for
    return [c for k in keys.split() for c in (NAMES[k] if k in NAMES else (ord(k),))]


def fbar(rows):  # "F1 help  F2 menu …" from the key table: what the F-keys do here
    fk = sorted((int(k[1:]), what.split()[0].rstrip(":")) for keys, what, m in rows if m
                for k in keys.split() if k[:1] == "F" and k[1:].isdigit())
    return "  ".join(f"F{n} {w}" for n, w in fk)


def lcwd():  # the local folder; if someone removed it underneath us, home
    try:
        return os.getcwd()
    except OSError:
        os.chdir(os.path.expanduser("~"))
        return os.getcwd()


class View:  # one list on screen: its rows (after the filter), cursor, scroll and filter
    def __init__(v):
        v.rows, v.sel, v.top, v.flt = [], 0, 0, ""


class Pane:  # a folder on some file system, or the host list to pick one
    def __init__(p):
        p.fs = p.host = None
        p.cwd, p.items, p.marks, p.closed, p.up = "", [], set(), set(), []  # up: the archives we're inside
        p.files, p.hostv, p.mode = View(), View(), "hosts"

    @property
    def v(p):
        return p.files if p.mode == "files" else p.hostv

    def cur(p):  # the row under the cursor
        return p.v.rows[p.v.sel] if p.v.rows else None

    def targets(p):  # what an action works on: the marked entries, else the one under the cursor
        if p.marks:
            return [(n, a) for n, a in p.items if n in p.marks]
        c = p.cur()
        return [c] if c and c[1] else []


class App:
    def __init__(s, scr, cfg):
        s.cfg, s.scr, s.msg, s.typing = cfg, scr, "", None
        s.panes, s.focus, s.n, s.mid = [Pane(), Pane()], 0, 1, 1
        s.local, s.remotes, s.hosts, s.c, s.cmds, s.theme_name = Local(), {}, {}, {}, {}, "default"
        s.dots, s.order = True, "name"  # show dotfiles; sort by
        curses.curs_set(0)
        curses.set_escdelay(25)
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass  # no -1 support: set_theme falls back to mono
        s.rows = {m: [(k, what, getattr(s, f) if f else None) for k, what, f in KEYS[m] + KEYS["both"]]
                  for m in ("files", "hosts")}
        s.keymap = {m: {c: fn for keys, _, fn in rows if fn for c in codes(keys)} for m, rows in s.rows.items()}
        s.log, s.working, s.frame, s.drawing = deque(maxlen=3), None, 0, False
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

    def load(s):
        try:
            c = config.read(s.cfg)
            s.c, s.hosts, s.cmds = c, config.hosts(c), config.table(c, "commands")
            s.theme_name = str(c.get("theme", s.theme_name))
        except (OSError, tomllib.TOMLDecodeError) as e:
            s.msg = f"config error: {e}"
        s.set_theme(s.theme_name)
        for p in s.panes:
            s.sync(p)

    def set_theme(s, name):
        s.theme_name = name if name in THEMES else "default"
        t = THEMES[s.theme_name]
        # ponytail: theme needs more colors than the terminal has (TERM=xterm/screen) -> mono, not garbled colors
        s.C = MONO
        if curses.has_colors() and max(map(max, t)) < curses.COLORS:
            try:
                for i, (f, b) in enumerate(t, 1):
                    curses.init_pair(i, f, b)
                s.C = [curses.color_pair(i) for i in range(1, len(t) + 1)]
            except curses.error:
                pass

    def cycle_theme(s):
        keys = list(THEMES)
        s.set_theme(keys[(keys.index(s.theme_name) + 1) % len(keys)])
        if s.cfg.exists():  # no config: for this session only, nothing written
            config.save(s.cfg, config.retheme(s.cfg.read_text(), s.theme_name))
        s.msg = f"theme: {s.theme_name}" + (" (mono: terminal lacks colors)" if s.C is MONO else "")

    def sync(s, p):  # each view its own filter; ponytail: a hosts filter unfolds every group, p.closed untouched
        hq, fq = p.hostv.flt.lower(), p.files.flt.lower()
        p.hostv.rows = [r for r in [(0, "local", LOCAL, ("local",)), *config.flat(s.hosts, set() if hq else p.closed)]
                        if config.match(r, hq)]
        items = sorted(p.items[1:], key=ORDER[s.order])  # stable: folders first, the chosen order within
        items.sort(key=lambda x: not isdir(x[1]))
        p.files.rows = [r for r in p.items[:1] + items if r[0] == ".." or fq in r[0].lower()
                        and (s.dots or r[0][:1] != ".")]

    # --- processes ---
    def busy(s, what):  # say what's going on; the spinner keeps turning by itself while a blocking call runs
        if not s.working:
            s.t0 = time.monotonic()
            signal.setitimer(signal.ITIMER_REAL, 0.12, 0.12)
        s.working = what
        s.draw()
        s.scr.refresh()

    def idle(s):
        signal.setitimer(signal.ITIMER_REAL, 0)
        s.working = None

    def spin(s, *_):  # SIGALRM: Python runs this between curses calls, never inside one; skip mid-frame
        if s.working and not s.drawing:
            s.frame += 1
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
        return s.local if h is LOCAL else s.remotes.setdefault(h["host"], Remote(h, s.term, s.busy))

    def ticker(s, label, total):  # progress in the status bar; copies report how far each file is
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
                s.busy(f"{label}  {human(tick.done)}{f' of {human(total)}' if total else ''}{left}  {human(rate)}/s{files}  ^C stops")
        tick.done, tick.finished = 0, set()
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

    # --- files ---
    def ls(s, p, d, keep=None):  # list folder d in pane p; keep: a name to put the cursor on
        s.busy(f"listing {safe(d) or '~'}")
        fs = p.fs
        d = fs.realpath(d)
        items = []
        for n, a in fs.listdir(d):
            if islink(a):  # a link to a folder browses like one (delete still removes only the link)
                try:
                    a = fs.stat(join(d, n))
                except OSError:
                    pass  # dangling: stays a plain entry
            items.append((n, a))
        if d != p.cwd:
            p.marks, p.files.flt, p.files.sel, p.files.top = set(), "", 0, 0  # another folder: a fresh start
        p.cwd, p.items = d, [("..", None), *items]
        if not p.up:
            p.host["_cwd"] = d
        s.sync(p)
        if keep:
            p.files.sel = next((i for i, r in enumerate(p.files.rows) if r[0] == keep), p.files.sel)

    def relist(s):  # both panes, after anything that may have changed what they show
        for p in s.panes:
            if p.fs:
                s.ls(p, p.cwd)

    def open_host(s, p, h):
        while p.up:
            s.leave(p)
        was = p.fs, p.host, p.mode, p.cwd
        p.fs, p.host, p.mode, p.cwd = s.fsfor(h), h, "files", ""
        try:
            s.ls(p, h.get("_cwd") or h.get("path", ""))  # back where you left this host
        except BaseException:
            p.fs, p.host, p.mode, p.cwd = was  # couldn't get in: the pane stays as it was
            raise

    def open(s):
        p = s.P
        c = p.cur()
        if not c:
            return
        if c[0] == "..":
            s.parent()
        elif isdir(c[1]):
            s.ls(p, join(p.cwd, c[0]))
        else:
            s.launch("open")

    def parent(s):
        p = s.P
        if p.up and p.cwd == "/":
            return s.ls(p, *s.leave(p))
        s.ls(p, p.cwd.rsplit("/", 1)[0] or "/", keep=p.cwd.rsplit("/", 1)[-1])  # cursor on where you came from

    def fetch(s, p, name, a):  # a private copy (0700 folder) of a file not on this machine -> (folder, file)
        d = tempfile.mkdtemp()
        lp = os.path.join(d, name)
        try:
            s.xfer(lambda t: copy(p.fs, join(p.cwd, name), s.local, lp, t, a), f"↓ {safe(name)}", a.get("size"), [p.fs])
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)
            raise
        return d, lp

    def enter(s, p, name, a):  # an archive, browsed like a folder; one not on this machine comes over first
        d, lp = (None, join(p.cwd, name)) if isinstance(p.fs, Local) else s.fetch(p, name, a)
        try:
            s.busy(f"reading {safe(name)}")
            ar = Archive(lp, f"{p.fs.name}:{join(p.cwd, name)}")
        except BaseException:
            d and shutil.rmtree(d, ignore_errors=True)
            raise
        p.up.append((p.fs, p.cwd, name, d))
        p.fs, p.cwd = ar, ""
        s.ls(p, "/")

    def leave(s, p):  # out of the archive we're in (its copy goes) -> (the folder it is in, its name)
        p.fs.close()
        p.fs, cwd, name, d = p.up.pop()
        if d:
            shutil.rmtree(d, ignore_errors=True)
        return cwd, name

    def cleanup(s):  # at exit: no archive copies left behind
        for p in s.panes:
            while p.up:
                s.leave(p)

    def mark(s):
        c = s.P.cur()
        if c and c[1]:
            s.P.marks ^= {c[0]}
            s.step(1)

    def markall(s):
        p = s.P
        p.marks = set() if p.marks else {n for n, a in p.files.rows if a}

    def markpat(s):  # + marks, - unmarks: every shown entry whose name matches a pattern like *.log
        p = s.P
        plus = s.key_hit != ord("-")
        pat = s.ask(f"{'mark' if plus else 'unmark'} names like:", "*")
        hit = {n for n, a in p.files.rows if a and fnmatch.fnmatchcase(n, pat)} if pat else set()
        p.marks = p.marks | hit if plus else p.marks - hit

    def dotfiles(s):
        s.dots = not s.dots
        for p in s.panes:
            s.sync(p)
        s.msg = f"dotfiles {'shown' if s.dots else 'hidden'}"

    def sort(s):
        s.order = list(ORDER)[(list(ORDER).index(s.order) + 1) % len(ORDER)]
        for p in s.panes:
            s.sync(p)
        s.msg = f"sorted by {s.order}"

    def chmod(s):  # the marked entries (or the one under the cursor) get the permissions typed, in octal
        p = s.P
        items = p.targets()
        if not items:
            return
        mode = s.ask("permissions (octal):", f"{stat.S_IMODE(items[0][1].get('perm', 0)):o}")
        if mode:
            try:
                m = int(mode, 8)
            except ValueError:
                raise OSError(f"{safe(mode)}: not octal, like 644") from None
            for n, _ in items:
                p.fs.chmod(join(p.cwd, n), m)
            s.relist()
            s.msg = f"✓ permissions {m:o}"

    def view(s):
        s.launch("view")

    def edit(s):
        s.launch("edit")

    def launch(s, how):  # ⏎ ("open"), view or edit the file under the cursor, by your tools or rshm's own
        p = s.P
        c = p.cur()
        if not c or not c[1] or isdir(c[1]):
            return
        name, a = c
        if how == "open":
            cmd = config.opener(s.c, name)
            if cmd:
                return s.external(p, name, a, cmd, False)
            if name.lower().endswith(ARCHIVES):
                return s.enter(p, name, a)
            how = "view"
        cmd = config.tool(s.c, how)
        if cmd:
            return s.external(p, name, a, cmd, how == "edit")
        size = a.get("size") or 0
        if size > BIG and not s.confirm(f"{safe(name)} is {human(size)}: load it all? (y/N)"):
            return
        s.busy(f"loading {safe(name)}")
        path = join(p.cwd, name)
        data = b"".join(p.fs.read(path, 0, size))
        s.idle()
        title = f"{p.fs.name}:{path}"
        with s.still():
            if how == "view":
                viewer.view(s.scr, data, title, s.C)
            else:
                editor.edit(s.scr, data, title, s.C, lambda b: save(p.fs, path, b))
        if how == "edit":
            s.relist()

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
                    s.xfer(lambda t: copy(s.local, lp, p.fs, path, t), f"↑ {safe(name)}", os.path.getsize(lp), [p.fs])
                except BaseException as e:
                    keep = True
                    raise OSError(f"not saved ({e or 'interrupted'}): your edit is in {safe(lp)}") from None
                s.msg = f"✓ saved {safe(name)}"
                s.relist()
        finally:
            if not keep:
                shutil.rmtree(d, ignore_errors=True)

    def copy(s):
        s.transfer(move=False)

    def move(s):
        s.transfer(move=True)

    def transfer(s, move):  # the marked entries (or the one under the cursor) to the other pane's folder
        p, o = s.P, s.O
        items = p.targets()
        if not items:
            return
        if o.mode != "files":
            raise OSError("the other pane shows no folder: pick one there (Tab, then ⏎ on a host)")
        if move and isinstance(p.fs, Archive):
            raise OSError("an archive is read-only: copy out of it (F5) instead")
        what = safe(items[0][0]) if len(items) == 1 else f"{len(items)} entries"
        dst = s.ask(f"{'move' if move else 'copy'} {what} to {o.fs.name}:", o.cwd)
        if not dst:
            return
        dst = dst if dst.startswith("/") else join(o.cwd, dst)
        at = peek(o.fs, dst)
        if at and islink(at):
            at = peek(o.fs, o.fs.realpath(dst))
        if at and isdir(at):  # into that folder
            there = dict(o.fs.listdir(dst))
            pairs = [(n, a, join(dst, n)) for n, a in items]
        elif len(items) == 1 and not at:  # a new name for the one entry
            there, pairs = {}, [(items[0][0], items[0][1], dst)]
        else:
            raise OSError(f"{safe(dst)}: not a folder")
        pairs = [x for x in pairs if not (o.fs is p.fs and join(p.cwd, x[0]) == x[2])]  # onto itself: nothing to do
        if not pairs:
            raise OSError("that is where it already is")
        clash = [n for n, _, t in pairs if t.rsplit("/", 1)[-1] in there]
        if clash and not s.confirm(f"{len(clash)} already there ({safe(clash[0])}…): overwrite, merging folders? (y/N)"):
            return
        verb = "moved" if move else "copied"
        try:
            if move and o.fs is p.fs:  # one file system: a rename, nothing travels
                for n, _, t in pairs:
                    s.busy(f"moving {safe(n)}")
                    p.fs.rename(join(p.cwd, n), t)
                s.msg = f"✓ {verb} {what}"
                return
            total = None if any(isdir(a) for _, a, _ in pairs) else sum(a.get("size", 0) for _, a, _ in pairs)
            gone = set()  # moved entries already removed at the source: a retry skips them

            def run(tick):
                for n, a, t in pairs:
                    if n not in gone:
                        copy(p.fs, join(p.cwd, n), o.fs, t, tick, a)
                        if move:
                            rm(p.fs, join(p.cwd, n))
                            gone.add(n)
            s.xfer(run, f"{'→' if s.focus == 0 else '←'} {what}", total, [p.fs, o.fs])
        finally:
            p.marks -= {n for n, _, _ in pairs}
            s.relist()

    def mkdir(s):
        p = s.P
        name = s.ask("new folder:").strip("/")
        if name:
            p.fs.mkdir(join(p.cwd, name))
            s.ls(p, p.cwd, keep=name.split("/")[0])

    def delete(s):
        p = s.P
        items = p.targets()
        what = safe(items[0][0]) if len(items) == 1 else f"{len(items)} entries"
        if items and s.confirm(f"delete {what}{'/' if len(items) == 1 and isdir(items[0][1]) else ''}? (y/N)"):
            try:
                for n, _ in items:
                    s.busy(f"deleting {safe(n)}")
                    rm(p.fs, join(p.cwd, n))
                    p.marks.discard(n)
                s.msg = f"✓ deleted {what}"
            finally:
                s.relist()  # show what's left even if it stopped halfway

    def rename(s):  # never overwrites; "sub/", "sub/x" or "/abs/x" move it on this file system, like mv
        p = s.P
        c = p.cur()
        if not c or not c[1]:
            return
        new = s.ask("rename to:", c[0])
        if new and new != c[0]:
            dst = new if new.startswith("/") else join(p.cwd, new)
            dst = join(dst, c[0]) if new.endswith("/") else dst
            p.fs.rename(join(p.cwd, c[0]), dst, replace=False)
            s.ls(p, p.cwd, keep=new)
            s.msg = f"✓ renamed {safe(c[0])} to {safe(new)}"

    def menu(s):  # F2: the [commands] from the config, run in this pane's folder
        if not s.cmds:
            raise OSError("no [commands] in your config: e in the host list edits it")
        names = list(s.cmds)
        i = s.choose([(n, "") for n in names])
        if i is not None:
            p, o, q = s.P, s.O, shlex.quote
            c = p.cur()
            sub = {"{name}": q(c[0]) if c and c[1] else "", "{names}": " ".join(q(n) for n, _ in p.targets()),
                   "{dir}": q(p.cwd), "{other}": q(o.cwd)}
            cmd = s.cmds[names[i]]
            for k, v in sub.items():
                cmd = cmd.replace(k, v)
            s.run(p, f"{cmd}; printf '\\n[exit %s] ⏎ ' $?; read _ _", p.cwd)

    def run(s, p, script, d):  # a shell command on the pane's machine, on the terminal; then show what changed
        cmd, cwd = p.fs.run(script, d)
        s.term(cmd, cwd)
        s.relist()

    def shell(s):
        s.run(s.P, SHELL, s.P.cwd)

    def goto(s):
        p = s.P
        d = s.ask("go to:", p.cwd)
        if d:
            s.ls(p, d)

    def home(s):
        s.ls(s.P, "")

    # --- host list ---
    def hostlist(s):  # F9: this pane to the host list and back
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

    def hopen(s):  # host: browse it here; group: fold / unfold
        p = s.P
        c = p.cur()
        if c and c[2]:
            s.open_host(p, c[2])
        elif c:
            p.closed ^= {c[3]}
            s.sync(p)

    def hclose(s):  # open group: fold; anything else: jump to its group
        p = s.P
        c = p.cur()
        if c and not c[2] and c[3] not in p.closed:
            p.closed.add(c[3])
            s.sync(p)
        elif c and len(c[3]) > 1:
            p.hostv.sel = next((i for i, r in enumerate(p.hostv.rows) if r[3] == c[3][:-1]), p.hostv.sel)

    def hshell(s):
        c = s.P.cur()
        if c and c[2]:
            fs = s.fsfor(c[2])
            cmd, cwd = fs.run(SHELL, c[2].get("_cwd") or c[2].get("path", ""))
            s.term(cmd, cwd)

    def editcfg(s):  # the config (from the sample, the first time) in your editor or rshm's own
        config.create(s.cfg)
        cmd = config.tool(s.c, "edit")
        if cmd:
            s.term(["sh", "-c", config.cmdline(cmd, str(s.cfg))])
        else:
            with s.still():
                editor.edit(s.scr, s.cfg.read_bytes(), str(s.cfg), s.C, lambda b: save(s.local, str(s.cfg), b))
        s.load()

    # --- everywhere ---
    def ping(s):  # live check: one round trip to the host itself, by the same route as a session (jumps included)
        p = s.P
        h = p.host if p.mode == "files" else (p.cur() or (0, 0, None))[2]
        if not h or h is LOCAL:
            return
        connect(h, s.term, s.busy, timeout=3)
        s.busy(f"live check {h['host']}")
        t0 = time.monotonic()
        rc, err = quiet([*argv(h, batch=True, timeout=3), h["host"], "true"])
        if rc:
            raise OSError(f"{h['host']}: {err}")
        s.msg = f"✓ {h['host']} answers ({(time.monotonic() - t0) * 1000:.0f} ms)"

    def reload(s):
        s.load()
        s.relist()

    def step(s, d):  # the cursor, d rows down (up if negative), kept inside the list
        v = s.P.v
        v.sel = max(0, min(len(v.rows) - 1, v.sel + d))

    def tab(s):
        s.focus ^= 1

    def esc(s):  # clear the filter, else the marks, else from the host list back to the folder
        p = s.P
        if p.v.flt:
            p.v.flt = ""
            s.sync(p)
        elif p.mode == "files" and p.marks:
            p.marks = set()
        elif p.mode == "hosts" and p.fs:
            p.mode = "files"

    def quit(s):
        sys.exit()

    def help(s, y=None, x=None):  # every key of this view; ⏎ runs the chosen one
        rows = s.rows[s.P.mode]
        i = s.choose([(what, keys) for keys, what, _ in rows], y, x)
        if i is not None and rows[i][2]:
            rows[i][2]()

    def search(s):  # filter this view: type to narrow, arrows move; ⏎ applies (never opens), Esc clears
        p = s.P
        v = p.v
        s.typing, v.flt = "/", ""
        curses.curs_set(1)
        try:
            while True:
                s.draw()
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

    # --- prompts ---
    def ask(s, q, text=""):  # the one-line input at the bottom, over the current screen
        with s.still():
            s.draw()
            return readline(s.scr, s.scr.getmaxyx()[0] - 1, q, text, s.C[2] | curses.A_BOLD)

    def confirm(s, q):  # one key: y says yes, anything else no
        s.typing = q
        try:
            with s.still():
                s.draw()
                return s.scr.getch() in (ord("y"), ord("Y"))
        except KeyboardInterrupt:
            return False
        finally:
            s.typing = None

    def choose(s, items, y=None, x=None):  # a menu of (label, keys) -> the index picked, or None
        H, W = s.scr.getmaxyx()
        if y is None:
            v = s.P.v
            y, x = v.sel - v.top + 1, s.focus * s.mid + 2
        w, h = min(max(len(l) + len(k) for l, k in items) + 6, W), min(len(items) + 2, H)
        y, x = max(0, min(y, H - h)), max(0, min(x, W - w))
        win = curses.newwin(h, w, y, x)
        win.keypad(True)
        win.bkgd(" ", s.C[0])
        i = 0
        while True:
            off = max(0, i - h + 3)  # scroll when the terminal is shorter than the menu
            win.erase()
            win.box()
            for r, (l, k) in enumerate(items[off:off + h - 2]):
                win.addnstr(r + 1, 1, f" {l}".ljust(w - 3 - len(k)) + k + " ", w - 2, s.C[1] if r + off == i else s.C[0])
            c = win.getch()
            if c in MOVES:
                i = max(0, min(len(items) - 1, i + MOVES[c])) if abs(MOVES[c]) > 1 else (i + MOVES[c]) % len(items)
            elif c in (10, 13, 32, curses.KEY_ENTER, curses.KEY_RIGHT):
                return i
            elif c == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, b = curses.getmouse()
                except curses.error:
                    continue
                if b & (LEFT_CLICK | RIGHT_CLICK):
                    r = my - y - 1
                    return r + off if x <= mx < x + w and 0 <= r < h - 2 else None
            else:
                return None

    # --- input ---
    def key(s, c):
        s.key_hit = c  # for actions on two keys (+ and -)
        if c == curses.KEY_MOUSE:
            s.mouse()
        elif c in MOVES:
            s.step(MOVES[c])
        elif fn := s.keymap[s.P.mode].get(c):
            fn()

    def mouse(s):
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return
        side = int(x >= s.mid)
        if b & (WHEEL_UP | WHEEL_DOWN):
            s.focus = side
            return s.step(-3 if b & WHEEL_UP else 3)
        if b & RIGHT_CLICK:
            s.focus = side
            return s.help(y, x)
        if not b & LEFT_CLICK or not 0 < y <= s.n:
            return
        p = s.panes[side]
        j = p.v.top + y - 1
        if j < len(p.v.rows):
            again = s.focus == side and j == p.v.sel
            s.focus, p.v.sel = side, j
            if again or p.mode == "hosts":  # a host or group answers the first click; a file the second
                s.open() if p.mode == "files" else s.hopen()

    # --- drawing ---
    def draw(s):
        s.drawing = True
        try:
            s.paint()
        finally:
            s.drawing = False

    def paint_work(s):  # the live line at the bottom of the activity panel: spinner, what, how long
        H, W = s.scr.getmaxyx()
        if s.working and H >= 8 and W >= 40:
            line = f" {SPIN[s.frame % len(SPIN)]} {safe(s.working)}  {time.monotonic() - s.t0:.0f}s"
            try:
                s.scr.addnstr(H - 2, 0, line.ljust(W - 1), W - 1, s.C[2] | curses.A_BOLD)
            except curses.error:
                pass

    def title(s, p, w):  # where the pane is; a long path keeps its end, the part that tells folders apart
        tail = (f" [{len(p.marks)}]" if p.marks and p.mode == "files" else "") + (f" /{p.v.flt}" if p.v.flt else "") + " "
        t = "hosts" if p.mode == "hosts" else safe(f"{p.fs.name}:{p.cwd}")
        return " " + screen.bar(t, tail, w - 3).rstrip() + " "

    def paint_pane(s, i, x, w, n):
        p, active = s.panes[i], i == s.focus
        v = p.v
        v.sel = min(v.sel, max(len(v.rows) - 1, 0))
        v.top = min(max(v.top, v.sel - n + 1), v.sel)
        sc = s.scr
        corner = "┌" if not i else "┬"
        sc.addnstr(0, x, f"{corner}─{s.title(p, w)}".ljust(w, "─") + ("┐" if i else ""), w + i, s.C[2 if active else 3])
        for r in range(n):
            j = r + v.top
            if i:
                sc.addstr(r + 1, x, "│", s.C[3])
            if j >= len(v.rows):
                continue
            cursor = j == v.sel
            if p.mode == "hosts":
                dep, k, h, path = v.rows[j]
                ico = ("●" if h is p.host else "○") if h else ("▸" if path in p.closed else "▾")
                text, at = f" {'  ' * dep}{ico} {safe(k)}", s.C[0 if h else 2]
            else:
                name, a = v.rows[j]
                d = name == ".." or isdir(a)
                size = "" if d else human(a.get("size"))
                if w >= 56 and a and a.get("mtime"):  # room for the date too
                    size = f"{size:>7}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(a['mtime']))}"
                mark = "*" if name in p.marks else " "
                text = f"{mark}{safe(name)}{'/' if d and name != '..' else ''}"
                text = text[:w - len(size) - 2].ljust(w - len(size) - 2) + size
                at = s.C[2] | curses.A_BOLD if mark == "*" else s.C[2 if d else 0]
            if cursor:
                at = s.C[1] if active else at | curses.A_UNDERLINE
            sc.addnstr(r + 1, x + i, text.ljust(w - 1), w - 1, at)
        if v.top:
            sc.addstr(1, x + w - 2 + i, "▲", s.C[2])
        if v.top + n < len(v.rows):
            sc.addstr(n, x + w - 2 + i, "▼", s.C[2])

    def paint(s):
        sc = s.scr
        sc.erase()
        H, W = sc.getmaxyx()
        if s.msg:  # every message lands in the activity log: nothing vanishes on the next key
            s.log.append(f"{time.strftime('%H:%M:%S')} {s.msg}")
            s.msg = ""
        if H < 8 or W < 40:
            sc.addnstr(0, 0, "rshm: terminal too small", W - 1)
            return
        logn = 3 if H >= 18 else 1  # activity panel lines
        n = s.n = H - 3 - logn  # rows between the borders
        mid = s.mid = W // 2
        try:
            s.paint_pane(0, 0, mid, n)
            s.paint_pane(1, mid, W - mid - 1, n)
            sc.addnstr(n + 1, 0, "└─ activity ".ljust(mid, "─") + "┴".ljust(W - mid - 1, "─") + "┘", W, s.C[3])
            keep = logn - bool(s.working)  # the live line, when there is one, takes the last row
            log = list(s.log)[-keep:] if keep else []
            for i, line in enumerate(log):
                sc.addnstr(H - 1 - len(log) - bool(s.working) + i, 0, f" {safe(line)}", W - 1, s.C[4 if "✗" in line else 3])
            s.paint_work()
            if s.typing:
                hint = "⏎ apply  Esc clear" if s.typing == "/" else ""
                q = f" / {s.P.v.flt}" if s.typing == "/" else f" {s.typing}"
                sc.addnstr(H - 1, 0, q.ljust(W - 1 - len(hint)) + hint, W - 1, s.C[2] | curses.A_BOLD)
                sc.move(H - 1, min(len(q), W - 2))
            else:
                sc.addnstr(H - 1, 0, f" {fbar(s.rows[s.P.mode])}".ljust(W - 1), W - 1, s.C[3])
        except curses.error:
            pass  # a cell off-screen mid-resize; next frame fixes it

    def act(s, fn, *a):  # run one action: errors are reported, the session stays
        t0 = time.monotonic()
        try:
            fn(*a)
        except KeyboardInterrupt:  # Ctrl-C mid-action cancels just the action
            s.msg = "✗ interrupted"
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
            s.act(s.hopen)
        if len(start) == 1:
            s.act(s.open_host, s.panes[0], LOCAL)
        while True:
            s.draw()
            c = s.scr.getch()  # Ctrl-C here (idle) quits
            s.act(s.key, c)
