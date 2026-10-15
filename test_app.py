#!/usr/bin/env python3
"""The whole app in this process, on a strict virtual terminal (a write past any edge is an error, also where the
app would hide one): scripted keys for the paths the pty test doesn't press, then a monkey of random keys, taps and
sizes (RSHM_SEED, RSHM_MONKEY steps). Jailed: no write lands outside a temp sandbox, nothing reaches the network."""
import builtins, collections, curses as real, os, random, re, shutil, tempfile, time, types, zipfile
from pathlib import Path

BOX = Path(tempfile.mkdtemp(prefix="rshm-app-")).resolve()
os.environ["HOME"] = str(BOX)  # rshm's config, ssh's config and the login sockets: all in the sandbox
for v in ("EDITOR", "VISUAL", "PAGER", "XDG_CONFIG_HOME"):
    os.environ.pop(v, None)
from rshm import edit, fs, screen, ui, view  # noqa: E402  (after HOME is set)
from rshm.screen import cell, cells  # noqa: E402

rnd = random.Random(int(os.environ.get("RSHM_SEED", 1)))
ERRORS = []  # every write past an edge, wherever the app would hide it


class Done(BaseException):  # the keys ran out: the app's loop ends here
    pass


class VT:  # a terminal (or a window on one) of H×W cells, its keys from a script
    def __init__(t, H, W, y=0, x=0, root=None):
        t.H, t.W, t.y0, t.x0, t.root = H, W, y, x, root or t
        if not root:
            t.keys, t.mouse, t.cells, t.kick, t.nd = collections.deque(), None, {}, 0, False

    def fail(t, what):
        ERRORS.append(f"{what} at {t.root.H}x{t.root.W}")
        raise real.error(f"{what}() returned ERR")

    def getmaxyx(t):
        return t.H, t.W

    def addstr(t, y, x, s, attr=0):
        if not (0 <= y < t.H and 0 <= x < t.W) or x + cells(s) > t.W:
            t.fail(f"addstr {y},{x} +{cells(s)}")
        for c in s:
            t.root.cells[t.y0 + y, t.x0 + x] = c
            x += cell(c)

    insstr = addstr  # (the same check: a cell past the edge is an error)

    def move(t, y, x):
        if not (0 <= y < t.H and 0 <= x < t.W):
            t.fail(f"move {y},{x}")

    def erase(t):
        t.root.cells = {k: v for k, v in t.root.cells.items() if not (t.y0 <= k[0] < t.y0 + t.H and t.x0 <= k[1] < t.x0 + t.W)}

    clear = erase

    def box(t):
        if t.H < 2 or t.W < 2:
            t.fail("box")

    def refresh(t, *_):  # ("kick", n) in the keys: ^C at the n-th screen update from then (mid-action)
        r = t.root
        if r.kick:
            r.kick -= 1
            if not r.kick:
                raise KeyboardInterrupt

    def bkgd(t, *_):
        pass

    noutrefresh = refresh
    keypad = bkgd

    def get_wch(t):
        r = t.root
        if not r.keys:
            r.final = r.text()  # the screen as the keys ran out: whatever was on it, a viewer too
            raise Done
        k = r.keys.popleft()
        if k == "^C":
            raise KeyboardInterrupt
        if k == ("err",):  # a read the terminal fails (a signal mid-read)
            raise real.error("no input")
        if isinstance(k, tuple) and k[0] == "kick":
            r.kick = k[1]
            return t.get_wch()
        if isinstance(k, tuple) and k[0] == "size":
            r.H, r.W = k[1:]
            return real.KEY_RESIZE
        if isinstance(k, tuple):  # a tap: (y, x, buttons)
            r.mouse = (0, k[1], k[0], 0, k[2])
            return real.KEY_MOUSE
        return k

    def nodelay(t, flag):  # a peek mid-action: only ("poll", key) in the script is typed by then
        t.root.nd = flag

    def getch(t):
        r = t.root
        if r.nd:
            return r.keys.popleft()[1] if r.keys and isinstance(r.keys[0], tuple) and r.keys[0][0] == "poll" else -1
        k = t.get_wch()
        return ord(k) if isinstance(k, str) else k

    def text(t):
        return "\n".join("".join(t.cells.get((y, x), " ") for x in range(t.W)) for y in range(t.H))


def newwin(h, w, y, x):
    if h < 1 or w < 1 or y + h > SCR.H or x + w > SCR.W:
        SCR.fail(f"newwin {h}x{w}@{y},{x}")
    return VT(h, w, y, x, SCR)


def getmouse():
    if SCR.mouse is None:
        raise real.error("no mouse event")
    m, SCR.mouse = SCR.mouse, None
    return m


nop = lambda *a, **k: None
FAKE = types.SimpleNamespace(**{k: getattr(real, k) for k in dir(real) if k.isupper()}, error=real.error, COLORS=256,
                             newwin=newwin, getmouse=getmouse, has_colors=lambda: True, color_pair=lambda i: i << 8,
                             curs_set=nop, set_escdelay=nop, mouseinterval=nop, mousemask=nop, start_color=nop,
                             use_default_colors=nop, init_pair=nop, raw=nop, noraw=nop, cbreak=nop, flushinp=nop,
                             endwin=nop, doupdate=nop, ungetch=lambda k: SCR.keys.appendleft(chr(k) if k < 256 else k))
for m in (ui, edit, view, screen):
    m.curses = FAKE


class Jail(fs.Local):  # this machine, but every write stays in the sandbox (through links too); reading is free
    def inside(s, *ps, real=False):
        for p in ps:
            q = os.path.realpath(p) if real else os.path.join(os.path.realpath(os.path.dirname(p) or "."), os.path.basename(p))
            if not q.startswith(str(BOX) + "/"):  # strictly inside: the sandbox itself is off limits too
                raise PermissionError(f"jail: {p}")

    def mkdir(s, p): s.inside(p); super().mkdir(p)
    def rmdir(s, p): s.inside(p); super().rmdir(p)
    def unlink(s, p): s.inside(p); super().unlink(p)
    def rename(s, a, b, replace=True): s.inside(a, b); super().rename(a, b, replace)
    def symlink(s, target, p): s.inside(p); super().symlink(target, p)
    def hardlink(s, src, p): s.inside(p); s.inside(src, real=True); super().hardlink(src, p)
    def chattr(s, p, ch): s.inside(p, real=True); super().chattr(p, ch)
    def write(s, p, chunks, off, ch, done): s.inside(p, real=True); super().write(p, chunks, off, ch, done)


def offline(*a, **k):
    raise ConnectionError("ssh: no network in this test")


RAN, REAL = [], []  # what went to the terminal: (command, folder); REAL: scripts that really run (printf x >> …)
ui.Local, ui.connect, fs.connect = Jail, offline, offline
ui.quiet = lambda cmd: (255, "ssh: no network in this test")
ui.App.term = lambda s, cmd, cwd=None: RAN.append((cmd, cwd)) or (
    __import__("subprocess").call(cmd, cwd=cwd) if cmd[-1].startswith(tuple(REAL)) else 0)
tempfile.tempdir = str(BOX / "tmp")
(BOX / "tmp").mkdir()
MADE = []  # every private copy rshm makes: each must be gone once it's done with
ui.tempfile = types.SimpleNamespace(mkdtemp=lambda: MADE.append(tempfile.mkdtemp()) or MADE[-1])
left = lambda: [m for m in MADE if os.path.exists(m)]
CFG = BOX / "rshm.toml"
SCR = VT(30, 120)


def start(*keys, H=30, W=120):  # a fresh rshm on a fresh screen, run until its keys run out -> the app
    global SCR
    SCR = VT(H, W)
    SCR.keys.extend(k for ks in keys for k in ([*ks] if isinstance(ks, str) and ks != "^C" else [ks]))
    app = ui.App(SCR, CFG)
    try:
        app.loop()
    except (Done, SystemExit):
        pass
    app.paint()
    return app


def more(app, *keys):  # the same rshm, more keys
    SCR.keys.extend(k for ks in keys for k in ([*ks] if isinstance(ks, str) and ks != "^C" else [ks]))
    try:
        while True:
            app.paint()
            app.act(app.key, SCR.getch())
    except (Done, SystemExit):
        pass
    app.paint()
    return app


def log(app):
    return "\n".join(app.log)


K = types.SimpleNamespace(**{n: getattr(real, f"KEY_{n}") for n in ("UP", "DOWN", "LEFT", "RIGHT", "ENTER", "HOME", "END",
                                                                   "NPAGE", "PPAGE", "DC", "IC", "BACKSPACE", "MOUSE")},
                          **{f"F{i}": real.KEY_F0 + i for i in range(1, 11)})
CLICK, RIGHT, UP, DOWN = real.BUTTON1_PRESSED, real.BUTTON3_PRESSED, real.BUTTON4_PRESSED, screen.WHEEL_DOWN
os.chdir(BOX)
W = BOX / "w"
(W / "sub").mkdir(parents=True)
(W / "a.txt").write_text("one\ntwo\nthree\n" * 40)
(W / "bin.dat").write_bytes(bytes(range(256)) * 20)
with zipfile.ZipFile(W / "z.zip", "w") as z:
    z.writestr("in/x.txt", "x")
CFG.write_text('[commands]\n"list" = "ls {names}"\n[hosts]\ng = ["nohost"]\nbad = { host = "x", path = 5 }\n')

# --- the editor: every key, at any size; nothing typed is ever lost to the screen ---
ed = (W / "a.txt").read_bytes()
saved = []
for H, Wd in ((30, 120), (5, 30), (2, 8), (1, 1)):
    SCR = VT(H, Wd)
    SCR.keys.extend(["x", K.ENTER, K.BACKSPACE, K.DC, K.HOME, "\x01", K.END, "\x05", K.UP, K.DOWN, K.NPAGE, K.PPAGE,
                     K.LEFT, K.RIGHT, (0, 2, UP), (0, 2, DOWN), (1, 3, CLICK), K.MOUSE, "\x0b", "\x0b", "\x15", "\x1a",
                     "\x17", *"two", "\n", "\x17", *"nope", "\n", "\x1f", *"3", "\n", "\x1f", "\x1b", "\x1c", *"two",
                     "\n", *"2", "\n", "\x1c", "\x1b", ("size", 3, 3), "\t", "日", ("size", H, Wd), "\x13", "\x18", "x",
                     "\x18", "n"])
    edit.edit(SCR, ed, "a.txt", ui.MONO, saved.append)
    assert saved[-1] and b"\t\xe6\x97\xa5" in saved[-1] and b"2" in saved[-1], saved[-1][:80]
SCR = VT(10, 40)
SCR.keys.extend(["z", "\x18", "y"])  # ^X on a change: y saves, and a save that fails keeps you editing
edit.edit(SCR, b"q", "q", ui.MONO, saved.append)
assert saved[-1] == b"zq"
SCR.keys.extend(["z", "\x13", "\x18", "y", "\x18", "n"])
edit.edit(SCR, b"q", "q", ui.MONO, lambda b: (_ for _ in ()).throw(OSError("disk full")))  # shown, not left

# --- the question line: edits in place (^W a word, ^K to the end, ^A ^E the ends), scrolls sideways when narrow ---
for H, Wd in ((5, 60), (2, 9)):
    SCR = VT(H, Wd)
    SCR.keys.extend(["\x17", *[K.LEFT] * 4, "\x0b", "X", "\x01", "\x05", "\n"])
    assert screen.readline(SCR, "q:", "one two three") == "one X"

# --- the viewer: every key, text and hex, at any size ---
for H, Wd in ((30, 120), (4, 10), (1, 1)):
    SCR = VT(H, Wd)
    SCR.keys.extend([*"12g", "G", "g", K.DOWN, K.NPAGE, " ", "b", "f", (0, 0, DOWN), (0, 0, UP), K.MOUSE, "w", "w",
                     "/", *"three", "\n", "n", "N", "?", *"one", "\n", "/", "\x1b", "/", *"zzz", "\n", "x", "/",
                     *"two", "\n", "?", *"one", "\n", "/", *"nope", "\n", "x", ("size", 2, 3), "j", "q"])
    view.view(SCR, (W / "a.txt").read_bytes() + bytes(range(32)) * 3, "a.txt", ui.MONO)
SCR = VT(10, 40)
SCR.keys.extend(["G", "x", "G", "q"])
view.view(SCR, b"", "empty", ui.MONO)

# --- the app: what the pty test doesn't press, each checked ---
os.chdir(W)
app = start()
assert "1  local:" in SCR.text() and "a.txt" in SCR.text(), SCR.text()
more(app, "@", K.DOWN, (2, 5, CLICK))  # a tap on a host only selects it (a login is never one stray tap away)
assert app.P.mode == "hosts" and app.P.cur()[2] is ui.LOCAL
more(app, (2, 5, CLICK))  # the second opens it
assert app.P.mode == "files"
more(app, "@", K.F5)  # on the host list, a file action asks for a host
assert "pick a host first" in log(app)
more(app, "\x1b", "f")  # history: nothing further
assert "no further" in log(app)
more(app, "@", "/", *"zzzz", "\n", "\n", "\x1b", "\x1b")  # ⏎ on an empty filtered list: nothing
more(app, "/", *"a.t", K.DOWN, K.UP, K.BACKSPACE, "\x00", "\n")  # the filter: arrows move, ⌫ edits
assert app.P.files.flt == "a." and app.P.cur()[0] == "a.txt"
more(app, "\x1b", "\x06", "\x15", "\n")  # ^F with no pattern: nothing
more(app, "\x06", "\n")  # ^F *: everything below, found
assert app.P.found == "*" and any(n == "sub" for n, _ in app.P.items)
assert "Esc goes back" in log(app)
more(app, "\x1b", "D")  # Esc: out of find results, back in the folder  # the other pane shows hosts: nothing to compare with
assert "the other pane shows no folder" in log(app)
more(app, K.DOWN, "c")
assert "the other pane shows no folder: pick one" in log(app)
assert not app.P.found
more(app, "/z.zip", "\n", "\n", "o")  # inside an archive: no mirror; F6 refused; @ and a host leaves it
assert "not from inside an archive" in log(app)
more(app, "\t", "\n", "\t", K.DOWN, K.F6)
assert "an archive is read-only" in log(app)
more(app, "@", K.HOME, "\n")
assert not app.P.up and "local:" in SCR.text()
more(app, "@", K.DOWN, "\n", K.DOWN, "h", "h", "\n", "s", "p", K.DOWN, "s", "p")  # ← in a group: to it; ^O, p
assert log(app).count("no network") == 2
more(app, "\n")  # opening a host that can't be reached: the pane stays as it was
assert "no network" in log(app) and app.P.mode == "hosts"
more(app, "\x1b", "\x1b")
assert app.P.mode == "files"
more(app, "\x1b", "/a.txt", "\n", "c", "\x15", "/", "\n")  # copy onto a folder you can't write: the jail says no
assert "jail" in log(app) or "Permission" in log(app)
more(app, "c", "\x15", str(W), "\n")  # onto itself
assert "already is" in log(app)
more(app, "c", "\x15", str(W / "bin.dat"), "\n")
assert "bin.dat: not a folder" in log(app)
more(app, "c", "\x15", str(W / "nope" / "x"), "\n")
assert "No such file" in log(app)
(W / "sub" / "a.txt").write_text("other")
more(app, "c", "\x15", str(W / "sub"), "\n", "n")  # a clash, and no: nothing
assert (W / "sub" / "a.txt").read_text() == "other"
more(app, "c", "\x15", "\n")  # an empty answer: nothing
more(app, "A", "\x15", *"zz", "\n")  # A: a form, one row per attribute; only the rows you change are applied
assert "mode zz: not like 6" in log(app)
more(app, "A", K.DOWN, "\x15", *"nobody:nogroup", "\n")
assert "✓ owner nobody:nogroup" in log(app) and (W / "a.txt").stat().st_uid != 0
mode = (W / "a.txt").stat().st_mode
more(app, "A", K.DOWN, "\x15", *"99999999:0", "\n", "A", K.DOWN, "\x15", *":root", "\n", "A", "\t", "\x15", *"nouser_x:", "\n")
assert "nouser_x: no user by that name here" in log(app) and (W / "a.txt").stat().st_mode == mode
more(app, "A", K.UP, "\x15", *"2001-09-09 01:46", K.UP, K.UP, "\x15", *"600", K.DOWN, K.DOWN, "\n")  # ↑ wraps round
assert (W / "a.txt").stat().st_mode & 0o777 == 0o600 and time.localtime((W / "a.txt").stat().st_mtime)[:5] == (2001, 9, 9, 1, 46)
more(app, "A", K.LEFT, K.LEFT, "\x7f", "0", K.HOME, "\x05", K.RIGHT, K.DC, "\x1b")  # the line edits in place; Esc: nothing
assert (W / "a.txt").stat().st_mode & 0o777 == 0o600
more(app, "R", "\x15", "\n", "L", "\x15", "\n", "H", "\x15", "\n", "z")  # empty answers: nothing
more(app, "/bin", "\n", K.F3, "q", K.F4, "\x18")  # binary opens as hex; edited as is
RAN.clear()
more(app, "/a.txt", "\n", K.F2, K.END, "\n")  # F2: what can be done here, then your [commands]
assert RAN and RAN[-1][0][-1].startswith("ls a.txt;"), RAN
CFG.write_text("theme = [\n")  # a broken config: said, the app goes on with what it had
more(app, "r")
assert "config:" in log(app)
CFG.write_text('theme = "amber"\n[tools]\nedit = "true"\nview = "true"\n')
more(app, "r", "@", K.F4, "\x1b", "e")  # your editor for the config; for a file: your tools
assert any("true" in str(c) for c, _ in RAN)
more(app, "\x1b", "/a.txt", "\n", "v", "e", "\n")
CFG.write_text("")
more(app, "r", "[", "]", (29, 5, UP), (29, 5, DOWN), (10, 1, RIGHT), "\x1b", (25, 100, RIGHT), "\x1b", (5, 3, DOWN),
     (5, 70, CLICK), (5, 70, CLICK), "\x1b", (0, 2, CLICK), K.RIGHT, (0, 30, CLICK), (0, 0, CLICK), (14, 60, CLICK),
     "\x1b", (1, 70, RIGHT), "\x1b", (29, 2, CLICK), "\x1b", (0, 119, CLICK), K.MOUSE, "?", K.DOWN, "\n", "\x1b")
more(app, K.F9, K.LEFT, "\n")  # a menu item in Pane 2's menu acts on pane 2
assert app.focus == 1
more(app, K.F9, (20, 100, CLICK), K.F9, K.DOWN, K.DOWN, K.NPAGE, K.PPAGE, (1, 2, UP), (1, 2, DOWN), K.END, "\x1b")
(W / "lnd").symlink_to("sub")  # onto a link to a folder: into the folder
for i in (0, 1):  # both panes here, the first one focused
    app.act(app.open_host, app.panes[i], ui.LOCAL, str(W))
app.focus = 0
more(app, "/bin.dat", "\n", "c", "\x15", str(W / "lnd"), "\n")
assert (W / "sub" / "bin.dat").exists()
more(app, "\x1b", "/a.txt", "\n", "L", "\x15", *str(W / "sub" / "ln"), "\n")  # L: one question, where; relative
assert os.readlink(W / "sub" / "ln") == "../a.txt" and (W / "sub" / "ln").read_text() == (W / "a.txt").read_text()
more(app, "L", "\n")  # the default: the other pane's folder (here the same), the same name: taken
assert "File exists" in log(app).split("\n")[-1]
more(app, "H", "\x15", *"sub/hl", "\n")  # H: another name; relative to this folder
assert (W / "sub" / "hl").stat().st_ino == (W / "a.txt").stat().st_ino and "another name for a.txt" in log(app)
more(app, "\x1b", "/sub", "\n", "\n", "/ln", "\n", "L", "\x15", *"hl", "\n", "h", "\x1b")  # L on a link: retarget
assert os.readlink(W / "sub" / "ln") == "hl"
more(app, "\x1b", K.HOME, "L", "H", "A")  # on ..: nothing
more(app, "/bin.dat", "\n", K.F2, K.END, "\n", "\x1b")  # F2, no [commands]: the file actions only; the last is z
assert log(app).split("\n")[-1].endswith("bin.dat: 5.0K in 1 files")
ui.connect = lambda *a, **k: None  # a host that's logged in but doesn't answer
CFG.write_text('[hosts]\ng = ["nohost"]\n')
more(app, "r", "@", "/nohost", "\n", "p", "\x1b", "\x1b", "/", ("err",), "x", "\n", "\x1b")
assert "nohost: ssh: no network" in log(app)
ui.connect = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("nohost: Permission denied (publickey)."))
more(app, "@", "/nohost", "\n", "p", "\x1b", "\x1b")  # p never asks for a login: it says one is wanted
assert "nohost answers, but wants a login" in log(app)
ui.connect = offline
more(app, "@", "/nohost", "\n", "p", "\x1b", "\x1b")
assert "nohost unreachable: ssh: no network" in log(app)
os.chdir(BOX)
(W / "sub" / "many").mkdir()
for i in range(120):
    (W / "sub" / "many" / str(i)).touch()
more(app, "\x1b", "\t", ("kick", 2), "\x06", "\n")  # ^C mid-find: what was found so far
assert "find stopped" in log(app)
more(app, "\x1b", "/sub", "\n", ("kick", 1), "z")  # ^C mid-action: just that action
assert "✗ stopped" in log(app)
more(app, "\x0c")  # ^L: the whole screen drawn again
assert "10quit" in SCR.text() and "1  local:" in SCR.text()
app.busy("a long one")  # the spinner turns by itself while a call blocks (its timer's tick, run here by hand)
spun = app.spun
app.spin()
assert app.spun == spun + 1 and "a long one" in SCR.text()
app.drawing = True  # never mid-frame
app.spin()
assert app.spun == spun + 1
app.drawing = False
app.idle()
more(app, "r", ("poll", 27))  # Esc while it runs: stopped there
assert log(app).endswith("✗ stopped")
more(app, K.END, "r", ("poll", ord("k")))  # anything else typed meanwhile: done after
assert app.P.files.sel == len(app.P.files.rows) - 2
more(app, "\x1b", "\x1b", "/sub", "\n", K.F8, "^C", "g", "^C")  # ^C at a question: no
assert (W / "sub").exists()
app.cleanup()

# --- a host (a real sftp-server, no jail: scripted keys only): view, your tools, an edit sent back, archives ---
SRV = next((x for x in ("/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/libexec/sftp-server")
            if os.path.exists(x)), None)
if SRV:
    from rshm import sftp  # noqa: E402
    sftp.argv = lambda h, batch=False, timeout=8: ["sh", "-c", f"exec {SRV}"]
    fs.connect = lambda h, *a: None
    (W / "rsub").mkdir()
    (W / "rsub" / "f").write_text("f")
    (W / "bad.zip").write_bytes(b"junk")
    (W / "huge").touch()
    os.truncate(W / "huge", 3 * ui.HEAD)  # 3 MB of zeros: binary
    CFG.write_text(f'[hosts]\nsrv = {{ host = "srv", path = "{W}" }}\n')
    app = start("@", "/srv", "\n", "\n", "/a.txt", "\n", K.F3, "q")
    assert app.P.fs.name == "srv" and app.P.cwd == str(W)
    CFG.write_text(f'[tools]\nview = "true"\nedit = "printf x >>"\n[hosts]\nsrv = {{ host = "srv", path = "{W}" }}\n')
    REAL.append("printf x >>")
    more(app, "r", "v", "e")  # your viewer gets a private copy; your editor's change goes back
    assert "✓ saved a.txt" in log(app) and (W / "a.txt").read_text().endswith("x")
    RAN.clear()  # binary, with a viewer set: never to it; rshm's hex view of the first MB; not edited
    more(app, "\x1b", "/huge", "\n", "v")
    assert "00000000  00 00" in SCR.final and "(the first 1.0M of 3.0M)" in SCR.final and not RAN
    more(app, "e")
    assert "huge is binary: v shows it" in log(app) and not RAN
    more(app, "\x1b", "/z.zip", "\n", "\n", "h", "\n")  # an archive there
    assert app.P.up
    app.cleanup()
    assert not app.P.up and not left()
    more(app, "r", "\x1b", "/bad.zip", "\n", "\n", "\x1b", "/a.txt", "\n", ("kick", 2), "v")  # not an archive; ^C fetching
    assert "not an archive I can read (file could not be opened successfully)" in log(app) and "stopped" in log(app) and not left()
    CFG.write_text(f'[tools]\nedit = "printf x >> {{file}}; rm -r {W / "rsub"}"\n[hosts]\nsrv = {{ host = "srv", path = "{W}" }}\n')
    REAL[:] = ["printf x >>"]
    more(app, "r", "\x1b", "/rsub", "\n", "\n", "/f", "\n", "e")  # an edit that can't go back: kept, and said where
    kept = next(Path(x) for x in re.findall(r"your edit is in (\S+)", log(app)))
    assert kept.read_text() == "fx"
    shutil.rmtree(kept.parent)
    more(app, "\x1b", "@", "\x1b", "/", "\x15", "\n")
    CFG.write_text("")
    REAL.clear()
    fs.connect = offline
    for r in app.remotes.values():
        r.close()
    os.chdir(BOX)
    (W / "huge").unlink()

# a big text file: rshm's viewer shows its first MB, its editor won't take it; ^C in a prompt cancels it;
(W / "big").write_bytes(b"line\n" * ui.HEAD)  # a terminal with few colours gets mono
os.chdir(W)
app = start("/big", "\n", "v")
assert "(the first 1.0M of 5.0M)" in SCR.final and "line" in SCR.final
more(app, K.F4, "\x1b", "\x1b", "g", "\x03", "t", "t", "t", "t", "t")
assert "big is 5.0M: more than rshm's editor takes (1.0M)" in log(app) and app.theme_name == "default"
os.chdir(BOX)
FAKE.COLORS = 8
app = start("t")
assert app.C is ui.MONO and "mono" in log(app)
FAKE.COLORS = 256
(W / "big").unlink()

# --- the monkey: random keys, taps and sizes; never an error that isn't an OSError, never a write off the screen ---
TYPES = {n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, BaseException)} | {"error"}
keys = [*"\n\t \x1b*+-.@[]~/abcdefghijklmnopqrstuvwxyzADHLRSz?\x06\x0f\x12\x15\x03", *(getattr(K, n) for n in vars(K))]
for run in range(int(os.environ.get("RSHM_MONKEY", 40))):
    shutil.rmtree(W, ignore_errors=True)
    (W / "sub" / "deep").mkdir(parents=True)
    for i in range(8):
        (W / f"f{i}.txt").write_text(f"file {i}\n" * i)
    (W / "ln").symlink_to("f1.txt")
    (W / "out").symlink_to("/etc")
    with zipfile.ZipFile(W / "z.zip", "w") as z:
        z.writestr("in/x.txt", "x")
    os.mkfifo(W / "pipe")
    CFG.write_text('[commands]\n"c" = "true {names}"\n[hosts]\nh = "nohost"\n')
    os.chdir(W)
    script = []
    for _ in range(400):
        r = rnd.random()
        script.append(("size", rnd.randrange(1, 60), rnd.randrange(1, 200)) if r < .03 else
                      (rnd.randrange(30), rnd.randrange(120), rnd.choice([CLICK, RIGHT, UP, DOWN])) if r < .12 else
                      rnd.choice(keys))
    app = start(*[k for k in script if k not in ("q", K.F10)])
    os.chdir(BOX)
    app.cleanup()
    bad = [ln for ln in app.log if re.search(rf"✗ ({'|'.join(TYPES)}): ", ln)]
    assert not bad, (run, bad)
    wrong = [e for e in ERRORS if (lambda h, w: h >= ui.MIN_H and w >= ui.MIN_W)(*map(int, e.split(" at ")[1].split("x")))]
    assert not wrong, (run, sorted(set(wrong))[:9])  # below the least size: only never a crash
    ERRORS.clear()
assert not left(), f"private copies left behind: {left()}"
os.chdir("/")
shutil.rmtree(BOX)
print("app ok")
