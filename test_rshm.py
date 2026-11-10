#!/usr/bin/env python3
"""Offline check of everything but the screen and ssh itself. Copies run against a real sftp-server (the binary
OpenSSH runs server-side), talked to directly: no network, no sshd."""
import curses, io, os, pwd, re, shutil, signal, subprocess, sys, tarfile, tempfile, time, types, zipfile
from pathlib import Path

from rshm import config, edit, fs, screen, sftp, ssh, ui, view

tomllib = config.tomllib  # None before Python 3.11: the config's checks then are that it's left unread, plainly

screen.ASCII = False  # these checks are of a UTF-8 terminal, whatever this machine's locale; ASCII has its own below

# config: a host is an ssh destination; a table is a group; a list is a group of destinations named as themselves
h = config.tree({"solo": "me@solo", "lab": ["pi", "box"],
                 "prod": {"web": "deploy@web.example.com", "db": {"host": "db", "path": "/srv/db"}}})
if tomllib:
    sample = tomllib.loads(config.SAMPLE)
    assert set(sample) == {"theme", "include", "tools", "open", "commands", "hosts"} and config.tool(sample, "edit") is None
    full = tomllib.loads(re.sub(r"^# ", "", config.SAMPLE, flags=re.M))  # every example in it, switched on, is right
    assert config.tree(full["hosts"]) == {"work": {"host": "work"}, "pi": {"host": "me@pi", "path": "/srv"},
                                          "lab": {"box": {"host": "box"}, "nas": {"host": "nas"}},
                                          "prod": {"web": {"host": "deploy@web.example.com"}}}
    assert config.opener(full, "a.DJVU") and "{names}" in config.table(full, "commands")["disk usage"]
assert config.SAMPLE in (Path(__file__).parent / "README.md").read_text()  # the README shows it, word for word
assert config.table({"commands": {"a": "x", "b": 3}}, "commands") == {"a": "x"} and config.table({"commands": "j"}, "commands") == {}

# tools: your [tools] choice, else the environment's ($VISUAL before $EDITOR), else None: rshm's own
for v in ("VISUAL", "EDITOR", "PAGER"):
    os.environ.pop(v, None)
assert config.tool({}, "edit") is None and config.tool({}, "view") is None
os.environ["EDITOR"], os.environ["PAGER"] = "vi", "less"
assert config.tool({}, "edit") == "vi" and config.tool({}, "view") == "less"
os.environ["VISUAL"] = "code -w"
assert config.tool({}, "edit") == "code -w" and config.tool({"tools": {"edit": "nvim"}}, "edit") == "nvim"
for v in ("VISUAL", "EDITOR", "PAGER"):
    os.environ.pop(v)
assert config.cmdline("nvim", "/a b") == "nvim '/a b'" and config.cmdline("bat {file} | less", "x") == "bat x | less"
op = {"open": {"*.pdf": "zathura", "*.7z *.RAR": "7z l {file}"}}  # [open]: first match, any case
assert config.opener(op, "A.PDF") == "zathura" and config.opener(op, "x.rar") == "7z l {file}" and not config.opener(op, "x.txt")
rows = list(config.flat(h, set()))
names = lambda rs: [r[1] for r in rs]
assert names(rows) == ["solo", "lab", "pi", "box", "prod", "web", "db"]
assert h["lab"] == {"pi": {"host": "pi"}, "box": {"host": "box"}} and h["prod"]["db"]["path"] == "/srv/db"
assert names(config.flat(h, {("lab",)})) == ["solo", "lab", "prod", "web", "db"]
assert config.tree({"web": 5, "g": {"x": []}, "l": ["ok", 3]}) == {"web": {}, "g": {"x": {}}, "l": {"ok": {"host": "ok"}}}
H = {r[1]: r[2] for r in rows if r[2]}

# filter: group name, host name or destination; "" keeps all
assert names(r for r in rows if config.match(r, "")) == names(rows)
assert names(r for r in rows if config.match(r, "deploy@")) == ["web"]

# rshm HOST: exact name, group/name, then substring
paths = lambda q: ["/".join(p) for p, _ in config.find(h, q)]
assert paths("web") == ["prod/web"] and paths("lab/pi") == ["lab/pi"] and paths("DB") == ["prod/db"]
assert paths("lab") == ["lab/pi", "lab/box"] and paths("nope") == [] and paths("we") == ["prod/web"]
assert paths("me@we") == paths("web:/x") == []  # a destination: never a part of a name
assert config.dest("me@box:/srv") == {"host": "me@box", "path": "/srv"} and config.dest("box") == {"host": "box"}
assert config.dest("ssh://me@box:2222") == {"host": "ssh://me@box:2222"} and config.dest("box:") == {"host": "box"}

# ssh argv: the ssh config does user/key/jump; rshm adds only its master and timeouts, once (ssh keeps the first)
a = ssh.argv(H["web"], batch=True, timeout=3)
assert a[0] == "ssh" and "BatchMode=yes" in a and [o for o in a if o.startswith("ConnectTimeout")] == ["ConnectTimeout=3"]
assert not [o for o in a if o.startswith(("User", "Port", "IdentityFile", "ProxyCommand"))]
cp = lambda x: next(o for o in ssh.argv(x) if o.startswith("ControlPath="))  # one master per destination, short path
assert cp(H["web"]) != cp(H["db"]) and cp(H["web"]) == cp({"host": "deploy@web.example.com"})
assert len(cp(H["web"])) - len("ControlPath=") - len(str(Path.home())) == len("/.ssh/rshm-") + 16
r = fs.Remote(H["web"], None, None)
r.login = lambda: None
assert r.run("ls", "/s rv")[0][-3:] == ["-t", "deploy@web.example.com", "exec sh -c 'cd '\"'\"'/s rv'\"'\"' && ls'"]
assert fs.Local().run("ls", "/tmp") == (["sh", "-c", "ls"], "/tmp")

# network failures (retry, don't prompt) vs refusals (a terminal login may help), as OpenSSH words them
for e in ("ssh: connect to host x port 22: Connection refused", "Connection to 127.0.0.1 port 2400 timed out",
          "kex_exchange_identification: Connection closed by remote host", "Connection closed by UNKNOWN port 65535",
          "ssh: Could not resolve hostname nope: Name or service not known", "read: Connection reset by peer"):
    assert ssh.unreachable(e), e
for e in ("bob@x: Permission denied (publickey,password).", "Host key verification failed."):
    assert not ssh.unreachable(e), e

# config file: created with the sample the first time; saved atomically, 600, a symlinked one stays a symlink
d = Path(tempfile.mkdtemp())
assert config.read(d / "new" / "rshm.toml") == {} and not (d / "new").exists()  # no config: fine, and nothing written
config.create(d / "new" / "rshm.toml")  # only when asked: the sample, private
assert (d / "new" / "rshm.toml").read_text() == config.SAMPLE and (d / "new" / "rshm.toml").stat().st_mode & 0o777 == 0o600
(d / "gen.toml").write_text('[hosts]\ns1 = ["nmt-s1", "it-s1"]\nmine = "theirs"\n')  # include: generated hosts join
(d / "inc.toml").write_text('include = ["gen.toml"]\n[tools]\nedit = "nvim"\n[hosts]\nmine = "me"\n')
inc = config.read(d / "inc.toml")
if tomllib:
    assert inc["hosts"] == {"s1": ["nmt-s1", "it-s1"], "mine": "me"} and inc["tools"] == {"edit": "nvim"}
    (d / "bad.toml").write_text("[hosts\n")  # broken: said plainly, as an OSError like any other
    try:
        config.read(d / "bad.toml")
        raise AssertionError("broken TOML read")
    except OSError as e:
        assert str(e).startswith("bad.toml: ") and config.unread(d / "bad.toml") == ""
else:  # no TOML here: the config is left unread, and said why; no config at all says nothing
    assert inc == {} and config.unread(d / "inc.toml").startswith("inc.toml: ignored, needs Python 3.11 (this is 3.")
assert config.unread(d / "none.toml") == ""
os.environ["XDG_CONFIG_HOME"] = str(d)
assert config.locate() == d / "rshm.toml"
del os.environ["XDG_CONFIG_HOME"]
real = d / "dotfiles" / "rshm.toml"
real.parent.mkdir()
real.write_text("old")
(d / "link.toml").symlink_to(real)
config.save(d / "link.toml", "theme = 'x'\n")
assert (d / "link.toml").is_symlink() and real.read_text() == "theme = 'x'\n" and real.stat().st_mode & 0o777 == 0o600
replace, os.replace = os.replace, lambda *_: (_ for _ in ()).throw(OSError("disk full"))
try:
    config.save(real, "never")
    raise AssertionError("save didn't fail")
except OSError:
    assert real.read_text() == "theme = 'x'\n" and not list(real.parent.glob(".*rshm-tmp"))  # old file intact, no litter
finally:
    os.replace = replace
for src in ('theme = "amber"   # c\n[hosts]', "theme='amber' # c\n[hosts]", "[hosts]"):  # only the value changes
    out = config.retheme(src, "matrix")
    assert out.count("theme") == 1 and ("# c" in out) == ("# c" in src)
    assert re.search(r'^theme\s*=\s*"matrix"', out, re.M) and (not tomllib or tomllib.loads(out)["theme"] == "matrix")

# ssh config import: aliases (several per line, key=value, quotes), Includes (relative, glob, cycles), no patterns
(d / "conf.d").mkdir()
(d / "config").write_text('Host web db\n  HostName x\nhost=jump\nHost *.corp !bad "q a"\nInclude conf.d/*.conf config\n')
(d / "conf.d" / "a.conf").write_text("Host inner\n  ProxyJump jump\nMatch host foo\nHost web\nInclude b.conf\n")
(d / "b.conf").write_text("Host nested\n")  # a relative Include is relative to ~/.ssh, wherever it appears
config.SSH_CONFIG = d / "config"
assert config.ssh_hosts(d / "config") == ["web", "db", "jump", "q a", "inner", "nested"]
(d / "b.conf").unlink()  # live group: aliases not already listed in rshm's own config
lh = config.hosts({"hosts": {"mine": "web", "g": ["jump"]}})
assert lh["~/.ssh/config"] == {"db": {"host": "db"}, "q a": {"host": "q a"}, "inner": {"host": "inner"}}
assert [p for p, _ in config.find(lh, "inner")] == [("~/.ssh/config", "inner")]
config.SSH_CONFIG = d / "missing"
assert config.hosts({}) == {} and config.hosts({"hosts": "junk"}) == {}
hs = config.hosts({"hosts": {"g": {"s": "me@x"}}})  # string hosts become tables once: per-host state survives
assert [x for *_, x, _ in config.flat(hs, set()) if x][0] is [x for *_, x, _ in config.flat(hs, set()) if x][0]

# SFTP wire format: raw (non-UTF-8) names round-trip; attrs skip what they don't use
raw = b"caf\xe9\n"
name = raw.decode(errors="surrogateescape")
assert sftp.pack(7, name) == b"\x00\x00\x00\x07\x00\x00\x00\x05" + raw
rd = sftp.Reader(sftp.pack(0x80000000 | 0xF) + (1536).to_bytes(8, "big") + sftp.pack(1, 2, 0o40755, 3, 4, 1, "k", "v", 9))
assert rd.attrs() == {"size": 1536, "uid": 1, "gid": 2, "perm": 0o40755, "mtime": 4} and rd.u32() == 9 and fs.isdir({"perm": 0o40755})
assert fs.islink({"perm": 0o120777}) and not fs.isdir({"perm": 0o120777})
one = object.__new__(sftp.SFTP)  # a server's refusal, in this machine's words: as a local error, or the bare fact
for code, text, want in ((2, "No such file", FileNotFoundError), (3, "Permission denied", PermissionError),
                         (4, "Failure", "failed on the server (SFTP gives no reason"), (5, "Bad message", "Bad message")):
    one.early = {1: (101, sftp.Reader(sftp.pack(code, text, "")))}
    try:
        one.wait(1)
        raise AssertionError("a refusal taken for an answer")
    except OSError as e:
        assert type(e) is want if isinstance(want, type) else str(e).startswith(want), (code, e)

# screen helpers: keys, sizes; hostile names can't drive the terminal
assert ui.codes("x Del") == [ord("x"), curses.KEY_DC] and 10 in ui.codes("Enter") and ui.codes("F5 c") == [curses.KEY_F5, 99]
assert [screen.human(n) for n in (None, 0, 1023, 1536, 5 * 1024 ** 5)] == ["", "0B", "1023B", "1.5K", "5120.0T"]
assert screen.safe("a\x1b[2Jb\nc" + name) == "a?[2Jb?ccaf??"
gone = Path(tempfile.mkdtemp())  # the local folder vanishing underneath us: home, never a crash
os.chdir(gone)
gone.rmdir()
assert ui.lcwd() == str(Path.home())

# one key table: every action exists, no key bound twice; menus, the F-key bar and --help come from it
bound = [c for _, keys, _, m in ui.KEYS if m for c in ui.codes(keys)]
assert len(bound) == len(set(bound)) and all(callable(getattr(ui.App, m, None)) for *_, m in ui.KEYS if m)
assert [f"F{n} {w}" for n, w, _ in ui.fkeys()] == ["F1 help", "F2 menu", "F3 view", "F4 edit", "F5 copy", "F6 move",
                                                  "F7 mkdir", "F8 delete", "F9 pulldown", "F10 quit"]  # mc's ten, mc's order
assert all(ui.menu_rows(m) for m in ui.MENUS) and {r[0] for r in ui.KEYS} - {None} == {"Pane", "File", "Mark", "Command", "Options"}
assert ui.menu_rows("Pane 1") == ui.menu_rows("Pane 2") and "copy to other pane" in ui.keys_help()
assert [g for g, _ in ui.grouped()] == ["File", "Mark", "Pane", "Command", "Options", "moving, mouse"]  # help, by menu
assert sum(len(rows) for _, rows in ui.grouped()) == len(ui.KEYS) and "\n Mark\n   Space Ins" in ui.keys_help()


def rgb(i):  # an xterm colour as (r, g, b): the 16 basics, the 6x6x6 cube, the greys
    basics = [(0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238), (205, 0, 205), (0, 205, 205),
              (229, 229, 229), (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255), (255, 0, 255),
              (0, 255, 255), (255, 255, 255)]
    v = [0, 95, 135, 175, 215, 255]
    return basics[i] if i < 16 else (v[(i - 16) // 36], v[(i - 16) // 6 % 6], v[(i - 16) % 6]) if i < 232 else (8 + 10 * (i - 232),) * 3


def contrast(a, b):  # WCAG's ratio, 1 to 21
    lum = lambda c: sum(k * (x / 255 / 12.92 if x <= 10 else ((x / 255 + 0.055) / 1.055) ** 2.4)
                        for k, x in zip((0.2126, 0.7152, 0.0722), rgb(c)))
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


for name, th in ui.THEMES.items():  # every theme: the same roles, the same meaning, and readable (the default theme is
    roles = dict(zip(ui.ROLES, ui.pairs(th)))  # the terminal's own colours: those are the user's to choose)
    assert len(roles) == len(ui.ROLES) and all(-1 <= c < 256 for x in roles.values() for c in x[:2]), name
    assert roles["hidden"] == roles["dim"] and roles["file"] == roles["norm"] and roles["stale"][0] == roles["err"][0]
    assert all(roles[r][2:] == ("b",) for r in ("acc", "err", "dir", "exe", "special", "ok")), name
    if name != "default":
        low = [(r, round(contrast(*roles[r][:2]), 1)) for r in ui.ROLES
               if contrast(*roles[r][:2]) < (3 if r in ("dim", "hidden") else 4.5)]
        assert not low, (name, low)  # text 4.5:1 at least, faint text 3:1 (WCAG)

# a terminal that isn't UTF-8: told by its locale (the first of LC_ALL, LC_CTYPE, LANG set), not by what Python makes
for env, utf in (({"LANG": "en_US.UTF-8"}, True), ({"LC_ALL": "C.utf8"}, True), ({"LC_CTYPE": "C.UTF-8"}, True),
                 ({"LANG": "C"}, False), ({"LANG": "POSIX"}, False), ({}, False), ({"LANG": "de_DE.ISO-8859-1"}, False),
                 ({"LC_ALL": "C", "LANG": "en_US.UTF-8"}, False), ({"LC_ALL": "", "LANG": "en_GB.UTF-8"}, True)):
    got = subprocess.run([sys.executable, "-c", "from rshm import screen; print(screen.ASCII)"], capture_output=True,
                         text=True, cwd=Path(__file__).parent, env={"PATH": os.environ["PATH"], **env}).stdout.strip()
    assert got == str(not utf), (env, got)
screen.ASCII = True  # then each character drawn is one ASCII one, and measured as one: nothing shifts
glyphs = "─│┌┐└┘├┤┬┴✓✗…→←↑↓·×●○▸▾▲▼⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
assert screen.plain(glyphs) == "-|++++++++" "+!~" "><^v" "-x*o>v^v" "|/-\\|/-\\|/" and screen.plain("日本 e\u0301") == "?? e?"
assert screen.cells("日本e\u0301") == 4 and screen.fit("very_long_name.tar.gz", 12, "mid") == "very_…tar.gz"
assert screen.plain(screen.fit("日本語のファイル名.txt", 11, "mid")) == "?????~?.txt" and ui.keys_help().isascii()
drawn = []
win = types.SimpleNamespace(addstr=lambda *a: drawn.append(a), insstr=lambda *a: drawn.append(a), getmaxyx=lambda: (5, 9))
w = screen.window(win)
w.addstr(0, 0, "┌─ ✓ 日", 7)
w.insstr(1, 2, "┘")
assert drawn == [(0, 0, "+- + ?", 7), (1, 2, "+")] and w.getmaxyx() == (5, 9)
screen.ASCII = False
assert screen.window(win) is win and screen.plain("┌✓") == "┌✓" and "Enter" in ui.keys_help()
old = curses.curs_set  # a terminal that can't hide its cursor (vt100): nothing breaks
curses.curs_set = lambda on: (_ for _ in ()).throw(curses.error("curs_set() returned ERR"))
screen.cursor(0)
curses.curs_set = old
assert set(ui.MONO) == set(ui.ROLES) and {fs.kind(*e) for e in (("x", {"perm": 0o100644}),)} <= set(ui.ROLES)

# what an entry is: one kind decides its colour, ls -F its marker; the ls -l line of it; what a sync would copy
assert [fs.kind(n, a) for n, a in (("..", None), ("d", {"perm": 0o40755}), ("x", {"perm": 0o100755}),
                                   ("l", {"perm": 0o120777, "dest": {"perm": 0o100644}}), ("l", {"perm": 0o120777, "dest": None}),
                                   ("a.TGZ", {"perm": 0o100644}), ("p.png", {"perm": 0o100644}), (".rc", {"perm": 0o100644}),
                                   ("f", {"perm": 0o10644}), ("t", {"perm": 0o100644}))] == \
    ["dir", "dir", "exe", "link", "stale", "archive", "media", "hidden", "special", "file"]
assert [fs.marker(a) for a in (None, {"perm": 0o40755}, {"perm": 0o120777}, {"perm": 0o100755}, {"perm": 0o10644},
                               {"perm": 0o140644}, {"perm": 0o100644})] == ["/", "/", "@", "*", "|", "=", ""]
assert ui.lsl("l", {"perm": 0o120777, "nlink": 1, "uid": 0, "gid": 0, "user": "root", "group": "root", "size": 4,
                    "mtime": 0, "target": "x"}).startswith("lrwxrwxrwx 1 root:root 4B ") and ui.lsl("l", {"perm": 0o120777, "target": "x"}).endswith("l -> x")
here = [("..", None), ("same", {"size": 1, "mtime": 5}), ("newer", {"size": 1, "mtime": 9}), ("bigger", {"size": 2, "mtime": 1}),
        ("only", {"size": 1}), ("dir", {"perm": 0o40755}), ("both", {"perm": 0o40755})]
there = [("same", {"size": 1, "mtime": 5}), ("newer", {"size": 1, "mtime": 1}), ("bigger", {"size": 1, "mtime": 1}),
         ("both", {"perm": 0o40755})]
assert ui.differ(here, there) == {"newer", "bigger", "only", "dir"}

# panes: the host list starts folded with this machine on top; filters are per view; `rshm y` unfolds around y
G = config.tree({"top": "me@top", "g": {"x": "me@x", "sub": {"y": "me@y"}}})
assert config.groups(G) == {("g",), ("g", "sub")}
app = ui.App.__new__(ui.App)
app.hosts, app.panes, app.focus, app.order, app.dots, app.rev = G, [ui.Pane(), ui.Pane()], 0, "name", True, False
p = app.panes[0]
p.closed, p.items = config.groups(G), [("..", None), ("x.log", {"size": 1}), ("y.txt", {"size": 2})]
app.sync(p)
assert names(p.hostv.rows) == ["local", "top", "g"] and p.hostv.rows[0][2] is ui.LOCAL
p.hostv.flt = "y"
app.sync(p)
assert names(p.hostv.rows) == ["y"] and [r[0] for r in p.files.rows] == ["..", "x.log", "y.txt"]
p.hostv.flt, p.files.flt = "", "log"
app.sync(p)
assert names(p.hostv.rows) == ["local", "top", "g"] and [r[0] for r in p.files.rows] == ["..", "x.log"]
app.pick(p, ("g", "sub", "y"))
assert p.hostv.rows[p.hostv.sel][1] == "y" and p.closed == set()
p.mode, p.files.flt = "files", ""
app.sync(p)
p.files.sel = 0
assert p.targets() == []  # .. is never acted on
p.marks = {"y.txt"}
assert p.targets() == [("y.txt", {"size": 2})]
p.items = [("..", None), ("b", {"size": 1, "mtime": 2}), (".h", {"size": 9, "mtime": 1}), ("a", {"size": 5, "mtime": 3}),
           ("d", {"perm": 0o40755}), ("z.c", {"size": 0}), ("y.a", {"size": 0})]  # folders first, then the order
for order, rev, want in (("name", False, "d .h a b y.a z.c"), ("size", False, "d .h a b z.c y.a"),
                         ("date", False, "d a b .h z.c y.a"), ("extension", False, "d .h a b y.a z.c"),
                         ("name", True, "d z.c y.a b a .h")):
    app.order, app.rev = order, rev
    app.sync(p)
    assert [r[0] for r in p.files.rows] == ["..", *want.split()], (order, rev)
app.order, app.rev = "name", False
app.dots = False
app.sync(p)
assert [r[0] for r in p.files.rows] == ["..", "d", "a", "b", "y.a", "z.c"]

# copies between any two file systems: local, and a host (a real sftp-server); resumed, permissions kept
srv = next((x for x in (shutil.which("sftp-server"), "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server",
                        "/usr/libexec/sftp-server") if x and os.path.exists(x)), None)
if srv:
    def session():  # an SFTP session straight to sftp-server, as ssh would hand it over
        x = sftp.SFTP.__new__(sftp.SFTP)
        x.err, x.n, x.early, x.ext, x.dropped = tempfile.TemporaryFile(), 0, {}, set(), set()
        x.p = subprocess.Popen([srv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=x.err)
        return x.init()
    R, Lo = fs.Remote({"host": "test"}, None, None), fs.Local()
    R.sf = s = session()
    L, D = d / "local", d / "remote"
    (L / "sub").mkdir(parents=True)
    D.mkdir()
    big = os.urandom(5 * 1024 * 1024 + 123)
    (L / "big.bin").write_bytes(big)
    (L / "sub" / "x.txt").write_text("x")
    (L / "loop").symlink_to(L)  # a link loop and a dangling link: copied as links (cp -P), nothing followed
    (L / "gone").symlink_to(d / "nowhere")
    os.mkfifo(L / "pipe")  # a named pipe would block forever if opened: never touched

    def ticker():  # the App's ticker contract: tick(path, position, resumed); .done and .finished
        def tick(path, at, resumed=False):
            tick.log.append((path, at, resumed))
            tick.done, tick.pos[path] = tick.done + at - tick.pos.get(path, 0), at
        tick.log, tick.pos, tick.done, tick.finished, tick.skipped = [], {}, 0, set(), []
        return tick
    seen = ticker()
    fs.copy(Lo, str(L), R, str(D / "t"), seen)  # local -> host
    assert (D / "t" / "big.bin").read_bytes() == big and (D / "t" / "sub" / "x.txt").read_text() == "x"
    assert seen.done == len(big) + 1 and os.readlink(D / "t" / "loop") == str(L) and os.readlink(D / "t" / "gone") == str(d / "nowhere")
    assert (D / "t" / "big.bin").stat().st_mtime == int((L / "big.bin").stat().st_mtime)  # times travel (cp -p)
    fs.copy(Lo, str(L), R, str(D / "t"), seen)  # a reconnect re-walks the tree: finished files aren't sent again
    assert seen.done == len(big) + 1 and len(seen.finished) == 4 and not list(D.rglob("*" + fs.PART))
    os.chmod(D / "t" / "sub" / "x.txt", 0o600)  # replacing: the source's permissions, not the replaced file's
    (L / "sub" / "x.txt").write_text("new")
    os.chmod(L / "sub" / "x.txt", 0o640)
    fs.copy(Lo, str(L / "sub" / "x.txt"), R, str(D / "t" / "sub" / "x.txt"), ticker())
    assert (D / "t" / "sub" / "x.txt").read_text() == "new" and (D / "t" / "sub" / "x.txt").stat().st_mode & 0o777 == 0o640
    got = ticker()  # host -> local resumes a partial, never counting resumed bytes twice; restarts a bogus one
    (d / ("dl" + fs.PART)).write_bytes(big[:1_000_000])
    fs.copy(R, str(D / "t" / "big.bin"), Lo, str(d / "dl"), got)
    assert (d / "dl").read_bytes() == big and got.log[0][1:] == (1_000_000, True) and got.done == len(big)
    (d / ("dl2" + fs.PART)).write_bytes(b"z" * (len(big) + 5))
    fs.copy(R, str(D / "t" / "big.bin"), Lo, str(d / "dl2"), ticker())
    assert (d / "dl2").read_bytes() == big
    (D / "t" / ("up.bin" + fs.PART)).write_bytes(big[:2_000_000])  # local -> host resumes too
    fs.copy(Lo, str(L / "big.bin"), R, str(D / "t" / "up.bin"), ticker())
    assert (D / "t" / "up.bin").read_bytes() == big and b"posix-rename@openssh.com" in s.ext
    fs.copy(R, str(D / "t"), R, str(D / "t2"), ticker())  # within one host: the server copies by itself
    assert (D / "t2" / "big.bin").read_bytes() == big and b"copy-data" in s.ext
    shutil.rmtree(D / "t2")
    R2 = fs.Remote({"host": "test"}, None, None)  # a server without that: a second session reads
    R2.sf = session()
    R2.sf.ext.discard(b"copy-data")
    R2.twin = lambda: (lambda x: setattr(x, "sf", session()) or x)(fs.Remote({"host": "test"}, None, None))
    fs.copy(R2, str(D / "t"), R2, str(D / "t2"), ticker())
    R2.close()
    assert (D / "t2" / "big.bin").read_bytes() == big and (D / "t2" / "sub" / "x.txt").read_text() == "new"
    fs.copy(Lo, str(L / "sub"), Lo, str(d / "lsub"), ticker())  # local -> local
    assert (d / "lsub" / "x.txt").read_text() == "new"
    os.chmod(D / "t" / "sub" / "x.txt", 0o750)  # downloads keep the source permissions
    fs.copy(R, str(D / "t" / "sub" / "x.txt"), Lo, str(d / "perm"), ticker())
    assert (d / "perm").stat().st_mode & 0o777 == 0o750
    (D / "t" / "ln").symlink_to("sub/x.txt")  # a copy onto a symlink writes through it, like cp
    (L / "sub" / "x.txt").write_text("thru")
    fs.copy(Lo, str(L / "sub" / "x.txt"), R, str(D / "t" / "ln"), ticker())
    assert (D / "t" / "ln").is_symlink() and (D / "t" / "sub" / "x.txt").read_text() == "thru"
    (D / "t" / "x.part").write_text("MYDATA")  # a user file named like a partial is not ours: never touched
    fs.copy(Lo, str(L / "sub" / "x.txt"), R, str(D / "t" / "x"), ticker())
    assert (D / "t" / "x").read_text() == "thru" and (D / "t" / "x.part").read_text() == "MYDATA"
    for f in (Lo, R):  # a part that isn't this file's (a stray, an older version): started over, never mixed in
        (D / "t" / ("x" + fs.PART)).write_bytes(b"zz")
        fs.copy(f, str(L / "sub" / "x.txt"), R, str(D / "t" / "x"), ticker())
        assert (D / "t" / "x").read_text() == "thru" and not (D / "t" / ("x" + fs.PART)).exists()
    for f in (R, Lo):  # recursive delete, on either side; a link inside is removed, never followed
        (D / "t2").mkdir(exist_ok=True)
        (D / "t2" / "out").symlink_to(L)
        fs.rm(f, str(D / "t2"))
        assert not (D / "t2").exists() and (L / "big.bin").exists()
    for f in (R, Lo):  # rename: replaces by default, refuses when told not to
        (D / "a").write_text("a")
        (D / "b").write_text("b")
        for make in (lambda: f.rename(str(D / "a"), str(D / "b"), replace=False), lambda: f.mkdir(str(D / "b")),
                     lambda: f.symlink("x", str(D / "b")), lambda: f.hardlink(str(D / "a"), str(D / "b"))):
            try:  # whatever creates a name that's taken says so (SFTP's own word is just "Failure")
                make()
                raise AssertionError("created over an existing name")
            except FileExistsError as e:
                assert "exists" in str(e) and (D / "b").read_text() == "b", e
        try:  # a failure for another reason stays what it is
            f.mkdir(str(D / "nope" / "x"))
            raise AssertionError("made in a folder that isn't there")
        except OSError as e:
            assert not isinstance(e, FileExistsError), e
        try:  # a missing file: the same error, the same words, here or over SFTP
            f.stat(str(D / "nope"))
            raise AssertionError("a stat of nothing")
        except FileNotFoundError as e:
            assert ui.reason(e).endswith("No such file or directory"), e
        f.rename(str(D / "a"), str(D / "b"))
        assert (D / "b").read_text() == "a" and not (D / "a").exists()
    (D / "t" / "rloop").symlink_to(D / "t")  # a host tree back down, with a link loop, a dangling link, a pipe
    (D / "t" / "rgone").symlink_to(d / "nowhere")
    os.mkfifo(D / "t" / "rpipe")
    fs.copy(R, str(D / "t"), Lo, str(d / "back"), ticker())
    assert (d / "back" / "big.bin").read_bytes() == big and os.readlink(d / "back" / "rloop") == str(D / "t")
    assert os.readlink(d / "back" / "rgone") == str(d / "nowhere") and not (d / "back" / "rpipe").exists()
    assert set(seen.skipped) == {str(L / "pipe")}  # what a tree copy left out, for a move to know
    assert not (D / "t" / "pipe").exists() and (d / "back" / "sub" / "x.txt").stat().st_mtime == int((D / "t" / "sub" / "x.txt").stat().st_mtime)
    for bad in (lambda: fs.copy(R, str(D / "t" / "rpipe"), Lo, str(d / "p"), ticker()),
                lambda: fs.copy(Lo, str(L / "pipe"), R, str(D / "p"), ticker())):
        try:
            bad()
            raise AssertionError("a pipe was opened")
        except OSError as e:
            assert "not a regular file" in str(e)
    me = pwd.getpwuid(os.getuid()).pw_name
    for f in (R, Lo):  # attributes: mode and owner (ids, and the names a listing shows); the modified time
        st = (D / "b").stat()
        f.chattr(str(D / "b"), {"perm": 0o640})
        a = dict(f.listdir(str(D)))["b"]
        assert (D / "b").stat().st_mode & 0o777 == 0o640 and a["mtime"] == int(st.st_mtime) and a["user"] == me
        assert (a["uid"], a["gid"]) == (st.st_uid, st.st_gid)
        f.chattr(str(D / "b"), {"uid": st.st_uid, "gid": st.st_gid})  # the owner it has: allowed for anyone
        if os.getuid() == 0:  # root: hand it over, and back
            f.chattr(str(D / "b"), {"gid": 1})
            assert (D / "b").stat().st_gid == 1 and (D / "b").stat().st_uid == st.st_uid
            f.chattr(str(D / "b"), {"uid": st.st_uid, "gid": st.st_gid})
        fs.save(f, str(D / "b"), b"saved\n" * 10000)
        assert (D / "b").read_bytes() == b"saved\n" * 10000 and (D / "b").stat().st_mode & 0o777 == 0o640
        fs.save(f, str(D / "b"), b"")
        assert (D / "b").read_bytes() == b"" and not list(D.glob("*" + fs.PART))
    for f in (R, Lo):  # links: make, read, retarget; another name for a file; free space; a listing with targets
        (D / "ln2").unlink(missing_ok=True)
        (D / "hl").unlink(missing_ok=True)
        f.symlink("b", str(D / "ln2"))
        f.hardlink(str(D / "b"), str(D / "hl"))
        assert f.readlink(str(D / "ln2")) == "b" and (D / "hl").stat().st_ino == (D / "b").stat().st_ino
        assert f.free(str(D)) > 0 and dict(fs.look(f, str(D)))["ln2"]["dest"]["size"] == 0
        assert dict(f.listdir(str(D)))["hl"]["nlink"] == 2 and dict(fs.look(f, str(D)))["ln2"]["target"] == "b"
        f.chattr(str(D / "b"), {"mtime": 1_000_000_000})
        assert (D / "b").stat().st_mtime == 1_000_000_000
    assert sorted(r for r, _ in fs.walk(Lo, str(D / "t"))) == sorted(str(x.relative_to(D / "t")) for x in (D / "t").rglob("*"))
    assert dict(fs.walk(R, str(D / "t")))["ln"]["target"] == "sub/x.txt"  # find results know their links' targets too
    (D / "many").mkdir()  # links asked in windows: more of them than are ever in flight, the dangling ones None
    for i in range(150):
        (D / "many" / f"l{i}").symlink_to("../b" if i % 2 else "nowhere")
    seen_as = lambda f: sorted((n, a["target"], (a["dest"] or {}).get("size")) for n, a in fs.look(f, str(D / "many")))
    assert seen_as(R) == seen_as(Lo) and not s.early
    assert sum(a["dest"] is None for _, a in fs.look(R, str(D / "many"))) == 75
    shutil.rmtree(D / "many")
    for mode in (0o444, 0o775):  # a copy here gets the exact mode, not one bent by the umask
        os.chmod(D / "b", mode)
        fs.copy(R, str(D / "b"), Lo, str(d / "exact"), ticker())
        assert (d / "exact").stat().st_mode & 0o777 == mode
        (d / "exact").unlink()  # a new file each time: a replaced one keeps its own mode
    os.chmod(D / "b", 0o644)
    ext = set(s.ext)  # a server without OpenSSH's extensions: no hard links, no free space, a rename that unlinks first
    s.ext -= {b"hardlink@openssh.com", b"statvfs@openssh.com", b"posix-rename@openssh.com"}
    (D / "a").write_text("new")
    R.rename(str(D / "a"), str(D / "b"))
    R.rename(str(D / "b"), str(D / "a"))  # nothing there to unlink first
    R.rename(str(D / "a"), str(D / "b"))
    assert (D / "b").read_text() == "new" and R.free(str(D)) is None
    try:
        R.hardlink(str(D / "b"), str(D / "h2"))
        raise AssertionError("a hard link without the extension")
    except OSError as e:
        assert "does not support hard links" in str(e)
    s.ext = ext
    assert not hasattr(R, "nope")  # only the primitives are the session's
    (D / "tdir").symlink_to(D / "t")  # Tab: what a typed path can go on to, on this machine or a host alike
    for f in (Lo, R):
        assert fs.complete(f, str(D), "t") == ["t/", "tdir/"] and fs.complete(f, str(D), "t/su") == ["t/sub/"]
        assert fs.complete(f, "/", str(D / "t") + "/sub/") == [str(D / "t" / "sub" / "x.txt")]
        assert fs.complete(f, str(D), "nope/x") == [] and fs.complete(f, str(D), "t/sub/x.txt/") == []
    assert fs.complete(Lo, "/", "~/") == fs.complete(Lo, "/", str(Path.home()) + "/") and fs.complete(R, "/", "~/x") == []
    tk = lambda *a: None  # pack from a host: the same archive as from here
    tk.skipped = []
    fs.pack(R, str(D), [("t", R.stat(str(D / "t"), False))], str(d / "r.tar.xz"), tk)
    with tarfile.open(d / "r.tar.xz") as tf:
        assert tf.extractfile("t/big.bin").read() == big and tf.getmember("t/ln").linkname == "sub/x.txt"
    chunks = s.read(str(D / "t" / "big.bin"), 0, len(big))  # a read stopped early leaves no replies behind
    next(chunks)
    chunks.close()
    assert s.realpath(str(D)) == str(D) and not s.early
    s.close()
    assert not R.alive()  # a dropped session: a transfer retries, and the next use reopens it
else:
    print("(no sftp-server here: copy tests skipped)")

# archives: zip and tar.gz browse like folders, read-only; files copy out byte for byte, folders implied by paths too
z = d / "a.zip"
with zipfile.ZipFile(z, "w") as zf:
    zf.writestr("top.txt", "top")
    zf.writestr("deep/er/x.bin", bytes(range(256)) * 300)
tz = d / "a.tar.gz"
with tarfile.open(tz, "w:gz") as tf:
    for n, data in (("top.txt", b"top"), ("deep/er/x.bin", bytes(range(256)) * 300)):
        ti = tarfile.TarInfo(n)
        ti.size, ti.mode = len(data), 0o640
        tf.addfile(ti, io.BytesIO(data))
for path in (z, tz):
    ar = fs.Archive(str(path), "x")
    assert sorted(n for n, _ in ar.listdir("/")) == ["deep", "top.txt"] and fs.isdir(ar.stat("/deep/er"))
    assert ar.realpath("") == "/" and ar.realpath("/deep/../top.txt") == "/top.txt"
    out = d / f"out-{path.name}"
    tick = lambda *a, **k: None
    tick.finished, tick.skipped = set(), []
    fs.copy(ar, "/", fs.Local(), str(out), tick)
    assert (out / "deep" / "er" / "x.bin").read_bytes() == bytes(range(256)) * 300 and (out / "top.txt").read_text() == "top"
    assert b"".join(ar.read("/deep/er/x.bin", 1000, 76800)) == (bytes(range(256)) * 300)[1000:]
    for bad in (lambda: ar.mkdir("/n"), lambda: ar.unlink("/top.txt"), lambda: fs.save(ar, "/top.txt", b"x")):
        try:
            bad()
            raise AssertionError("an archive was written")
        except OSError as e:
            assert "read-only" in str(e)
    ar.close()
src = d / "srcdir"  # tar -C dir . (entries "./x"), a hard link inside a tar, a symlink inside a zip (zip -y)
(src / "sub").mkdir(parents=True)
(src / "sub" / "a.txt").write_text("AAA")
os.link(src / "sub" / "a.txt", src / "hard.txt")
with tarfile.open(d / "dot.tgz", "w:gz") as tf:
    tf.add(src, arcname=".")
ar = fs.Archive(str(d / "dot.tgz"), "x")
assert sorted(n for n, _ in ar.listdir("/")) == ["hard.txt", "sub"]
hard = "/hard.txt" if ar.stat("/hard.txt")["ref"].islnk() else "/sub/a.txt"
assert ar.stat(hard)["size"] == 3 and b"".join(ar.read(hard, 0, 3)) == b"AAA"
zi = zipfile.ZipInfo("ln")
zi.external_attr = (0o120777 << 16)
with zipfile.ZipFile(d / "ln.zip", "w") as zf:
    zf.writestr("t.txt", "target")
    zf.writestr(zi, "t.txt")
ar = fs.Archive(str(d / "ln.zip"), "x")
assert ar.readlink("/ln") == "t.txt" and ar.stat("/ln")["size"] == 6 and dict(fs.look(ar, "/"))["ln"]["target"] == "t.txt"
assert fs.peek(ar, "/nope") is None and fs.peek(ar, "/t.txt")["size"] == 6 and ar.alive()
assert fs.complete(ar, "/", "") == ["ln", "t.txt"] and fs.complete(ar, "/", "/t") == ["/t.txt"]  # inside an archive too
# pack: a tree into each kind, by name, from any file system; it reads back as it was (links, modes, times), pipes left out
(src / "ln").symlink_to("sub/a.txt")
os.mkfifo(src / "fifo")
os.chmod(src / "sub" / "a.txt", 0o751)
os.utime(src / "sub" / "a.txt", (1_000_000_000, 1_000_000_000))
(src / "big.bin").write_bytes(os.urandom(300_000))
for ext in fs.ARCHIVES:
    if ext in fs.SINGLE:  # one file only, below
        continue
    tick = lambda p, at: seen.__setitem__(p, at)
    seen, tick.skipped = {}, []
    out = str(d / f"p{ext}")
    fs.pack(fs.Local(), str(d), [("srcdir", fs.Local().stat(str(src), False))], out, tick)
    assert tick.skipped == [str(src / "fifo")] and seen[str(src / "big.bin")] == 300_000, (ext, tick.skipped)
    pa = fs.Archive(out, "p")
    assert sorted(n for n, _ in pa.listdir("/srcdir")) == ["big.bin", "hard.txt", "ln", "sub"], ext
    assert pa.readlink("/srcdir/ln") == "sub/a.txt" and b"".join(pa.read("/srcdir/ln", 0, 3)) == b"AAA"
    a = pa.stat("/srcdir/sub/a.txt")
    assert a["perm"] & 0o777 == 0o751 and a["mtime"] == 1_000_000_000, (ext, a)
    assert b"".join(pa.read("/srcdir/big.bin", 0, 300_000)) == (src / "big.bin").read_bytes()
    tick.skipped = []  # from inside an archive: repacked
    fs.pack(pa, "/srcdir", [("sub", pa.stat("/srcdir/sub"))], str(d / "re.zip"), tick)
    pa.close()
    with zipfile.ZipFile(d / "re.zip") as zf:
        assert zf.read("sub/a.txt") == b"AAA" and zf.getinfo("sub/").is_dir()
for ext in fs.SINGLE:  # x.gz, x.bz2, x.xz: one file, packed, browsed and read as the file it holds; damage reported
    out, tick.skipped = str(d / f"big.bin{ext}"), []
    fs.pack(fs.Local(), str(src), [("big.bin", fs.Local().stat(str(src / "big.bin")))], out, tick)
    one = fs.Archive(out, "x")
    assert [(n, a["size"]) for n, a in one.listdir("/")] == [("big.bin", 300_000)] and fs.single(out) and not fs.single("a.tar.gz")
    assert b"".join(one.read("/big.bin", 1000, 300_000)) == (src / "big.bin").read_bytes()[1000:]
    one.close()
    blob = bytearray(Path(out).read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    Path(out).write_bytes(blob)
    try:
        fs.Archive(out, "x")
        raise AssertionError("damage passed on")
    except OSError as e:
        assert "unreadable archive" in str(e), e
    try:
        fs.pack(fs.Local(), str(d), [("srcdir", fs.Local().stat(str(src)))], out, tick)
        raise AssertionError("a folder in one file")
    except OSError as e:
        assert f"holds one file; for more, name it .tar{ext}" in str(e), e
inner = str(src / "sub" / ("in.tar" + fs.PART))  # packed into the very tree it packs: never itself
fs.pack(fs.Local(), str(src), [("sub", fs.Local().stat(str(src / "sub")))], inner, tick)
with tarfile.open(inner) as tf:
    assert tf.getnames() == ["sub", "sub/a.txt"], tf.getnames()
os.unlink(inner)
for x in ("fifo", "ln", "big.bin"):
    os.unlink(src / x)
os.chmod(src / "sub" / "a.txt", 0o644)
for bad in (lambda: ar.stat("/nope"), lambda: ar.readlink("/t.txt")):
    try:
        bad()
        raise AssertionError("no error")
    except OSError:
        pass
zb = (d / "ln.zip").read_bytes()  # the size it claims, more than it holds: an error, not a short file (nor a hang)
cd = zb.rindex(b"PK\x01\x02")
(d / "long.zip").write_bytes(zb[:cd + 24] + (999).to_bytes(4, "little") + zb[cd + 28:])
try:
    b"".join(fs.Archive(str(d / "long.zip"), "x").read("/t.txt", 0, 999))
    raise AssertionError("a short entry read as whole")
except OSError as e:
    assert "truncated in archive" in str(e) or "damaged" in str(e), e
for name, mode in (("d.tgz", "w:gz"), ("d.txz", "w:xz"), ("d.zip", None)):  # damage: always a clean error
    blob = os.urandom(200_000)
    (d / "f").write_bytes(blob)
    if mode:
        with tarfile.open(d / name, mode) as tf:
            tf.add(d / "f", arcname="f")
    else:
        with zipfile.ZipFile(d / name, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(d / "f", "f")
    b = (d / name).read_bytes()
    (d / name).write_bytes(b[:len(b) // 2] + bytes(500) + b[len(b) // 2 + 500:])
    try:
        ar = fs.Archive(str(d / name), name)
        assert b"".join(ar.read("/f", 0, len(blob))) == blob, "damage went unnoticed"
    except OSError as e:
        assert name in str(e) or "damaged" in str(e), e


class Shy(fs.Local):  # a folder it may not read: walk goes on without it
    def listdir(s, d):
        if d.endswith("/sub"):
            raise PermissionError(d)
        return super().listdir(d)


assert sorted(r for r, _ in fs.walk(Shy(), str(src))) == ["hard.txt", "sub"]
try:  # but the folder a find starts in, unreadable, is an error, not "nothing found"
    list(fs.walk(Shy(), str(src / "sub")))
    raise AssertionError("an unreadable start went unnoticed")
except PermissionError:
    pass
assert view.text(b"a\r\nb\tc\r\n\xff") == ["a", "b       c", "\ufffd"] and view.text(b"x\n") == ["x"]
(d / "junk.zip").write_bytes(b"not an archive")
try:
    fs.Archive(str(d / "junk.zip"), "junk.zip")
    raise AssertionError("junk opened")
except OSError as e:
    assert "unreadable archive" in str(e)

# text in cells: wide 2, combining 0; fitted exactly, cut with … at the start (paths, bars) or the middle (names)
assert screen.cells("a日\u0301") == 3 and screen.span("a日本b", 1, 3) == "日 " and screen.wrap("日本語abc", 4) == ["日本", "語ab", "c"]
assert screen.fit("very_long_name.tar.gz", 12, "mid") == "very_…tar.gz" and screen.fit("/a/long/path", 8) == "…ng/path"
assert screen.fit("日本語のファイル名.txt", 11, "mid") == "日本…名.txt" and screen.fit("ok", 4) == "ok  " and screen.fit("x\x1b", 3) == "x? "
assert all(screen.cells(screen.fit(s, w, k)) == w for s in ("日本語のファイル", "abc", "e\u0301te") for w in (1, 2, 5, 9)
           for k in ("end", "mid"))
assert screen.bar(" /a/long/path", " keys ", 14) == "…g/path keys " and screen.bar(" t", " k ", 10) == " t     k "
# layout: side by side; stacked when the terminal stands upright (a cell is about twice as tall as wide);
# one pane when two don't fit; nothing below the least size
side, stack = ui.layout(30, 120), ui.layout(40, 60)  # row 0 is the menu bar
assert [r[2] for r in side[0]] == [0, 60] and [r[1] for r in stack[0]] == [1, 18] and side[1] == stack[1] == 3
assert ui.layout(8, 44)[0] == [(None, 1, 0, 4, 44)] and ui.layout(6, 80) is None and ui.layout(30, 19) is None
assert ui.layout(10, 100)[0][1][2] == 50 and ui.layout(12, 30)[0][1][1] == 5 and len(ui.layout(7, 30)[0]) == 1  # each falls back to the other, then to one
# viewer: hex rows, wrap, search (forward, backward, wrapping round)
assert view.hexrow(b"AB\x00" + bytes(range(20)), 0).startswith("00000000  41 42 00 00 01 02 03 04  05 06")
assert view.hexrow(b"AB\x00", 0).endswith("|AB.|") and view.hexrow(bytes(17), 1).startswith("00000010  00 ")
assert view.rows(["abcdef", ""], 4, True) == [(0, "abcd"), (0, "ef"), (1, "")] and view.rows(["abcdef"], 4, False) == [(0, "abcdef")]
rs = ["one", "Two", "three", "two"]
assert view.find(4, rs.__getitem__, "two", 2, 1) == 3 and view.find(4, rs.__getitem__, "two", 0, 1) == 1
assert view.find(4, rs.__getitem__, "two", 0, -1) == 3 and view.find(4, rs.__getitem__, "zz", 0, 1) is None

# editor buffer: edits, undo (a run of typing is one step), find; bytes, tabs and CRLF come back exactly
raw = b"a\tb\r\nc\xff\xfe\r\n"
b = edit.Buffer(raw)
assert b.nl == "\r\n" and b.data() == raw
b.move(dy=1)
for ch in "xy":
    b.insert(ch)
assert b.lines[1] == "xyc\udcff\udcfe" and (b.y, b.x) == (1, 2)
b.newline()
b.insert("z")
assert b.lines[1:3] == ["xy", "zc\udcff\udcfe"]
b.backspace()
b.backspace()  # at a line's start: joins it to the one above
assert b.lines[1] == "xyc\udcff\udcfe" and (b.y, b.x) == (1, 2)
b.move(dx=-2)
b.delete()
assert b.lines[1] == "yc\udcff\udcfe"
while b.undos:
    b.undo()
assert b.data() == raw and b.changed
b = edit.Buffer(b"one\ntwo\none\n")
assert b.find("ONE") and (b.y, b.x) == (2, 0) and b.find("one") and (b.y, b.x) == (0, 0) and not b.find("zz")
b.move(dx=-1)  # left of the first character stays put; right of a line's end goes down
assert (b.y, b.x) == (0, 0)
b.move(dx=3)
b.move(dx=1)
assert (b.y, b.x) == (1, 0)
assert edit.Buffer(b"").data() == b"" and edit.Buffer(b"x\n").lines == ["x", ""]
assert edit.show("a\tb") == "a       b" and edit.show("日\tx") == "日      x"  # tabs to their stops, in cells
b = edit.Buffer(b"a\tbc\nl2\nl3")
b.goto(0, 9)  # a tap: the screen column to the character there
assert b.x == 3
b.cut()  # ^K ^K: two lines cut together, ^U puts them back; ^Z undoes each
b.cut()
assert b.lines == ["l3"] and b.clip == ["a\tbc", "l2"]
b.paste()
assert b.lines == ["a\tbc", "l2", "l3"]
b.undo()
b.undo()
b.undo()
assert b.data() == b"a\tbc\nl2\nl3"
b = edit.Buffer(b"one")
b.cut()  # the last line cut: an empty text, and back
assert b.lines == [""]
b.undo()
assert b.data() == b"one"
b = edit.Buffer(b"Foo foo\nbar FOO")
assert b.replace("foo", "x") == 3 and b.lines == ["x x", "bar x"]  # ^\: all of them, any case, one undo
b.undo()
assert b.lines == ["Foo foo", "bar FOO"] and b.replace("zz", "y") == 0

# saving through a link writes its target; a folder whose mode can't be set still gets its content; a lost link
(d / "real.txt").write_text("old")  # in the middle of a tree is an error, never "done"
(d / "via").symlink_to("real.txt")
fs.save(fs.Local(), str(d / "via"), b"new")
assert (d / "via").is_symlink() and (d / "real.txt").read_text() == "new"


class Fussy(fs.Local):  # a folder that's not yours: its mode stays; or the link drops at one step
    def __init__(s, err, at):
        s.err, s.at = err, at

    def chattr(s, p, ch):
        if os.path.isdir(p) and s.at == "chattr":
            raise s.err(p)
        super().chattr(p, ch)

    def mkdir(s, p):
        if s.at == "mkdir":
            raise s.err(p)
        super().mkdir(p)


tk = lambda *a, **k: None
tk.finished, tk.skipped = set(), []
fs.copy(fs.Local(), str(src), Fussy(PermissionError, "chattr"), str(d / "fussy"), tk)
assert (d / "fussy" / "sub" / "a.txt").read_text() == "AAA"
for at, dst in (("mkdir", "new"), ("chattr", "new2"), (None, "real.txt")):  # a drop; a folder onto a file
    try:
        tk.finished = set()
        fs.copy(fs.Local(), str(src), Fussy(ConnectionError, at), str(d / dst), tk)
        raise AssertionError("no error")
    except (ConnectionError, FileExistsError) as e:
        assert isinstance(e, ConnectionError) == bool(at), e

# the command line: errors before any screen; the single file it writes of itself
import contextlib  # noqa: E402
from rshm import __main__ as cli  # noqa: E402


real_terminal, tty = cli.terminal, (sys.stdin, sys.stdout)  # the terminal, every way it can be (test_live.py: real ones)


def term(known, cup=True, istty=True, name="xterm-kitty"):  # terminal() with terminfo knowing these -> note, or exit
    sys.stdin = sys.stdout = types.SimpleNamespace(isatty=lambda: istty)
    os.environ["TERM"] = name

    def setupterm():
        if os.environ["TERM"] not in known:
            raise curses.error("setupterm: could not find terminal")
    cli.curses = types.SimpleNamespace(setupterm=setupterm, tigetstr=lambda c: b"\x1b[H" if cup else None,
                                       error=curses.error)
    try:
        return real_terminal()
    except SystemExit as e:
        return e.code
    finally:
        sys.stdin, sys.stdout = tty


was = os.environ.get("TERM", "")
assert term({"xterm-kitty"}) == "" and term({"xterm-256color"}) == "TERM=xterm-kitty unknown, using xterm-256color"
assert os.environ["TERM"] == "xterm-256color" and term(set()).startswith("rshm: no terminfo for TERM=xterm-kitty")
assert term({"dumb"}, cup=False, name="dumb") == "rshm: TERM=dumb cannot address the cursor"
assert term({"xterm"}, istty=False) == "rshm: not a terminal"
os.environ["TERM"] = was
cli.terminal = lambda: ""  # below, main() in this process: no terminal here


def run(*args, wrapper=None):  # rshm ARGS in this process -> (exit code, stderr)
    sys.argv, err = ["rshm", *args], io.StringIO()
    cli.curses = types.SimpleNamespace(wrapper=wrapper or (lambda f: None), error=curses.error)
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            cli.main()
        return 0, err.getvalue()
    except SystemExit as e:
        return e.code, err.getvalue() + (e.code if isinstance(e.code, str) else "")


cfg = d / "c.toml"
cfg.write_text('[hosts]\nweb = "w"\nweb2 = "w2"\n')
assert "no such file" in run("-c", str(d / "none.toml"))[1] and "at most two hosts" in run("a", "b", "c")[1]
if tomllib:
    assert "ambiguous host 'we': web, web2" in run("-c", str(cfg), "we")[1] and run("-c", str(cfg), "zz")[0] == 0  # in no list: a destination
    assert run("-c", str(cfg), "web")[0] == 0 and run("-c", str(cfg), "web", "web2")[0] == 0
    (d / "bad.toml").write_text("[hosts\n")
    assert "bad.toml" in run("-c", str(d / "bad.toml"), "x")[1]
else:  # a config asked for by name, on a Python that can't read it: refused, saying why
    assert "c.toml: ignored, needs Python 3.11" in run("-c", str(cfg), "web")[1]
assert run(wrapper=lambda f: (_ for _ in ()).throw(KeyboardInterrupt))[0] == 0  # ^C at the very end: a quiet exit
assert "rshm: setupterm" in run(wrapper=lambda f: (_ for _ in ()).throw(curses.error("setupterm")))[1]
assert run("--pyz", str(d / "r.pyz"))[0] == 0 and os.access(d / "r.pyz", os.X_OK)
with zipfile.ZipFile(d / "r.pyz") as zf:
    assert {"__main__.py", "rshm/ui.py", "rshm/fs.py"} <= set(zf.namelist()) and b"3, 9" in zf.read("__main__.py")
with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):
    sys.argv = ["rshm", "-V"]
    sys.modules.pop("rshm.__main__")  # run as python -m rshm runs it
    __import__("runpy").run_module("rshm", run_name="__main__")
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.signal(signal.SIGHUP, signal.SIG_DFL)

# an unreadable ~/.ssh/config: no aliases from it, rshm still runs; ids with no name show as numbers
was, config.SSH_CONFIG = config.SSH_CONFIG, d  # a folder: exists, can't be read
assert config.hosts({"hosts": {"a": "b"}}) == {"a": {"host": "b"}}
config.SSH_CONFIG = was
assert fs.names(0, 4_000_000_000)[1] == "4000000000" and fs.names(4_000_000_000, 0)[0] == "4000000000"

# logging in: one try, on the terminal (a gpg or ssh prompt gets it whole, once); no ConnectTimeout of ours
calls = []
ssh.quiet = lambda cmd: calls.append(cmd) or (0 if cmd[-2:] == ["check", "up"] else 255, "denied")
ssh.login = lambda cmd: calls.append(cmd) or (0 if cmd[-1] == "z" else 255, "ssh: Could not resolve hostname x")
handed = []
tty = lambda fn, *a: handed.append((fn, a[0][-1])) or fn(*a)
for host, ok in (("up", True), ("x", False), ("z", True)):
    try:
        ssh.connect({"host": host}, tty, lambda m: None)
        assert ok, host
    except OSError as e:
        assert not ok and host in str(e), (host, e)
assert ssh.LOGINS == [{"host": "z"}] and handed == [(ssh.login, "x"), (ssh.login, "z")]  # "up" was in already
assert not any("ConnectTimeout" in " ".join(c) for c in calls if c[-2:] == ["-N", "z"])
try:  # asked not to ask (the live check): batch, detached, its own timeout; the refusal is the answer
    ssh.connect({"host": "w"}, tty, lambda m: None, timeout=3, ask=False)
    raise AssertionError("asked")
except ConnectionError as e:
    assert "denied" in str(e) and handed[-1] == (ssh.quiet, "w") and "ConnectTimeout=3" in calls[-1]
ssh.LOGINS.clear()

# progress: how far (0-1, for the bar), how fast, how long still, once a quarter second at most
said, clock = [], iter(range(0, 1000, 1))
ui.time = types.SimpleNamespace(monotonic=lambda: next(clock), strftime=time.strftime, localtime=time.localtime)
shown = types.SimpleNamespace(busy=lambda what, done=None, numbers="": said.append((what, done, numbers)),
                              clock=lambda: ui.time.monotonic())
tk = ui.App.ticker(shown, "↑ x", 4096)
tk("a", 1024, resumed=True)
tk("a", 2048)
tk.finished.add("a")
tk("b", 1024)
assert said[-1][:2] == ("↑ x", 0.75) and said[-1][2].startswith("3.0K of 4.0K") and said[-1][2].endswith("   1 file"), said
assert [screen.dur(x) for x in (0, 42, 185, 4213)] == ["0s", "42s", "3m05s", "1h10m"]

now = [0.0]  # the time left follows the link's speed (a moving average over time), not the average since the start
ui.time = types.SimpleNamespace(monotonic=lambda: now[0], strftime=time.strftime, localtime=time.localtime)
said.clear()
tk = ui.App.ticker(shown, "→ big", 1_000_000)
left = lambda: int(re.search(r"(\d+)s left", said[-1][2])[1]) + 60 * int((re.search(r"(\d+)m\d+s left", said[-1][2]) or [0, 0])[1])
for k in range(1, 41):  # 1 KB/s for 40 s
    now[0] = k
    tk("f", 1000 * k)
slow = left()
assert said[-1][1] == 0.04 and abs(slow - 960) < 5 and "1000B/s" in said[-1][2], said[-1]  # 960 KB to go at 1 KB/s
for k in range(41, 61):  # then 10 KB/s: within 20 s the estimate has caught up (an all-time average would say ~3 min)
    now[0] = k
    tk("f", 40_000 + 10_000 * (k - 40))
assert left() < 100 and re.search(r"  (8|9|10)\.\dK/s", said[-1][2]), said[-1]
said.clear()
tk = ui.App.ticker(shown, "→ x", 100)
now[0] += 1
tk("f", 50, resumed=True)  # nothing moved yet, only found there: no guess at the time left
assert said[-1][1] == 0.5 and "left" not in said[-1][2], said[-1]
now[0] = 0  # paused 100 s at 10 B/s: back from it, still 10 B/s (the pause isn't a slow link)
tk = ui.App.ticker(shown, "→ y", 1000)
now[0] = 1
tk("f", 10)
shown.clock, now[0] = lambda: now[0] - 100, 102  # what App.clock gives: paused time left out
tk("f", 20)
assert said[-1][2].startswith("20B of 1000B   10B/s"), said[-1]
shown.clock = lambda: ui.time.monotonic()

app = types.SimpleNamespace(busy=lambda *a: None, msg="", clock=lambda: ui.time.monotonic())  # the end: what, how much, how long, how fast
app.ticker = lambda label, total: ui.App.ticker(app, label, total)
now[0] = 0


def slow_copy(tick):
    for k in range(1, 186):
        now[0] = k
        tick("f", 8000 * k)


said = ui.App.xfer(app, slow_copy, "copying tree", 8000 * 185, [])
ui.time = time
assert said == "  1.4M in 3m05s, 7.8K/s", said

(d / "m" / "in").mkdir(parents=True)  # what a copy moves: the files' bytes; not links, pipes, folders
(d / "m" / "in" / "a").write_bytes(b"x" * 1000)
(d / "m" / "b").write_bytes(b"y" * 24)
(d / "m" / "ln").symlink_to("b")
os.mkfifo(d / "m" / "p")
counter = types.SimpleNamespace(busy=lambda *a: None, doing=None)
assert ui.App.measure(counter, fs.Local(), str(d), [("m", fs.Local().stat(str(d / "m")))]) == (1024, 2)
assert ui.App.measure(counter, fs.Local(), str(d / "m"), [("b", fs.Local().stat(str(d / "m" / "b"))), ("ln", fs.Local().stat(str(d / "m" / "ln"), False))]) == (24, 1)

shutil.rmtree(d)
print("ok")
