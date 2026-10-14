#!/usr/bin/env python3
"""Offline check of everything but the screen and ssh itself. Copies run against a real sftp-server (the binary
OpenSSH runs server-side), talked to directly: no network, no sshd."""
import curses, io, os, pwd, shutil, subprocess, tarfile, tempfile, tomllib, zipfile
from pathlib import Path

from rshm import config, edit, fs, screen, sftp, ssh, ui, view

# config: a host is an ssh destination; a table is a group; a list is a group of destinations named as themselves
h = config.tree(tomllib.loads("""
[hosts]
solo = "me@solo"
lab = ["pi", "box"]
[hosts.prod]
web = "deploy@web.example.com"
db = { host = "db", path = "/srv/db" }
""")["hosts"])
sample = tomllib.loads(config.SAMPLE)
assert config.tree(sample["hosts"]) == {"localhost": {"host": "localhost"}} and "{names}" in config.table(sample, "commands")["disk usage"]
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

# ssh argv: the ssh config does user/key/jump; rshm adds only its master and timeouts, once (ssh keeps the first)
a = ssh.argv(H["web"], batch=True, timeout=3)
assert a[0] == "ssh" and "BatchMode=yes" in a and [o for o in a if o.startswith("ConnectTimeout")] == ["ConnectTimeout=3"]
assert not [o for o in a if o.startswith(("User", "Port", "IdentityFile", "ProxyCommand"))]
cp = lambda x: next(o for o in ssh.argv(x) if o.startswith("ControlPath="))  # one master per destination, short path
assert cp(H["web"]) != cp(H["db"]) and cp(H["web"]) == cp({"host": "deploy@web.example.com"})
assert len(cp(H["web"])) - len("ControlPath=") - len(str(Path.home())) == len("/.ssh/rshm-") + 16
r = fs.Remote(H["web"], None, None)
r.login = lambda: None
assert r.run("ls", "/s rv")[0][-3:] == ["-t", "deploy@web.example.com", "cd '/s rv' && ls"]
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
assert inc["hosts"] == {"s1": ["nmt-s1", "it-s1"], "mine": "me"} and inc["tools"] == {"edit": "nvim"}
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
    assert tomllib.loads(out)["theme"] == "matrix" and out.count("theme") == 1 and ("# c" in out) == ("# c" in src)

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

# screen helpers: keys, sizes; hostile names can't drive the terminal
assert ui.codes("x Del") == [ord("x"), curses.KEY_DC] and 10 in ui.codes("⏎") and ui.codes("F5 c") == [curses.KEY_F5, 99]
assert [screen.human(n) for n in (None, 0, 1023, 1536, 5 * 1024 ** 5)] == ["", "0B", "1023B", "1.5K", "5120.0T"]
assert screen.safe("a\x1b[2Jb\nc" + name) == "a?[2Jb?ccaf??"
gone = Path(tempfile.mkdtemp())  # the local folder vanishing underneath us: home, never a crash
os.chdir(gone)
gone.rmdir()
assert ui.lcwd() == str(Path.home())

# one key table: every action exists, no key bound twice in a view; the F-key bar and --help come from it
for mode in ("files", "hosts"):
    bound = [c for keys, _, m in ui.KEYS[mode] + ui.KEYS["both"] if m for c in ui.codes(keys)]
    assert len(bound) == len(set(bound)), mode
assert all(callable(getattr(ui.App, m, None)) for rows in ui.KEYS.values() for _, _, m in rows if m)
assert ui.fbar(ui.KEYS["files"] + ui.KEYS["both"]) == \
    "F1 help  F2 menu  F3 view  F4 edit  F5 copy  F6 move  F7 new  F8 delete  F9 hosts  F10 quit"
assert "copy to the other pane" in ui.keys_help() and "F8 x Del" in ui.keys_help()

# panes: the host list starts folded with this machine on top; filters are per view; `rshm y` unfolds around y
G = config.tree({"top": "me@top", "g": {"x": "me@x", "sub": {"y": "me@y"}}})
assert config.groups(G) == {("g",), ("g", "sub")}
app = ui.App.__new__(ui.App)
app.hosts, app.panes, app.focus, app.order, app.dots = G, [ui.Pane(), ui.Pane()], 0, "name", True
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
           ("d", {"perm": 0o40755})]  # folders first, then by name, size or date; dotfiles can hide
for order, want in (("name", "d .h a b"), ("size", "d .h a b"), ("date", "d a b .h")):
    app.order = order
    app.sync(p)
    assert [r[0] for r in p.files.rows] == ["..", *want.split()], order
app.dots = False
app.sync(p)
assert [r[0] for r in p.files.rows] == ["..", "d", "a", "b"]

# copies between any two file systems: local, and a host (a real sftp-server); resumed, permissions kept
srv = next((x for x in (shutil.which("sftp-server"), "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server",
                        "/usr/libexec/sftp-server") if x and os.path.exists(x)), None)
if srv:
    def session():  # an SFTP session straight to sftp-server, as ssh would hand it over
        x = sftp.SFTP.__new__(sftp.SFTP)
        x.err, x.n, x.early, x.ext = tempfile.TemporaryFile(), 0, {}, set()
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
    (L / "loop").symlink_to(L)  # a link loop and a dangling link: both skipped, neither hangs nor fails
    (L / "gone").symlink_to(d / "nowhere")
    os.mkfifo(L / "pipe")  # a named pipe would block forever if opened: never touched

    def ticker():  # the App's ticker contract: tick(path, position, resumed); .done and .finished
        def tick(path, at, resumed=False):
            tick.log.append((path, at, resumed))
            tick.done, tick.pos[path] = tick.done + at - tick.pos.get(path, 0), at
        tick.log, tick.pos, tick.done, tick.finished = [], {}, 0, set()
        return tick
    seen = ticker()
    fs.copy(Lo, str(L), R, str(D / "t"), seen)  # local -> host
    assert (D / "t" / "big.bin").read_bytes() == big and (D / "t" / "sub" / "x.txt").read_text() == "x"
    assert seen.done == len(big) + 1 and not (D / "t" / "loop").exists() and not (D / "t" / "gone").exists()
    fs.copy(Lo, str(L), R, str(D / "t"), seen)  # a reconnect re-walks the tree: finished files aren't sent again
    assert seen.done == len(big) + 1 and len(seen.finished) == 2 and not list(D.rglob("*" + fs.PART))
    os.chmod(D / "t" / "sub" / "x.txt", 0o600)  # replacing keeps the target's permissions
    (L / "sub" / "x.txt").write_text("new")
    fs.copy(Lo, str(L / "sub" / "x.txt"), R, str(D / "t" / "sub" / "x.txt"), ticker())
    assert (D / "t" / "sub" / "x.txt").read_text() == "new" and (D / "t" / "sub" / "x.txt").stat().st_mode & 0o777 == 0o600
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
    os.chmod(D / "t" / "sub" / "x.txt", 0o750)  # downloads keep the source permissions (less umask)
    fs.copy(R, str(D / "t" / "sub" / "x.txt"), Lo, str(d / "perm"), ticker())
    assert (d / "perm").stat().st_mode & 0o777 == 0o750 & ~os.umask(os.umask(0))
    (D / "t" / "ln").symlink_to("sub/x.txt")  # a copy onto a symlink writes through it, like cp
    (L / "sub" / "x.txt").write_text("thru")
    fs.copy(Lo, str(L / "sub" / "x.txt"), R, str(D / "t" / "ln"), ticker())
    assert (D / "t" / "ln").is_symlink() and (D / "t" / "sub" / "x.txt").read_text() == "thru"
    (D / "t" / "x.part").write_text("MYDATA")  # a user file named like a partial is not ours: never touched
    fs.copy(Lo, str(L / "sub" / "x.txt"), R, str(D / "t" / "x"), ticker())
    assert (D / "t" / "x").read_text() == "thru" and (D / "t" / "x.part").read_text() == "MYDATA"
    for f in (R, Lo):  # recursive delete, on either side; a link inside is removed, never followed
        (D / "t2").mkdir(exist_ok=True)
        (D / "t2" / "out").symlink_to(L)
        fs.rm(f, str(D / "t2"))
        assert not (D / "t2").exists() and (L / "big.bin").exists()
    for f in (R, Lo):  # rename: replaces by default, refuses when told not to
        (D / "a").write_text("a")
        (D / "b").write_text("b")
        try:
            f.rename(str(D / "a"), str(D / "b"), replace=False)
            raise AssertionError("rename overwrote")
        except OSError:
            pass
        f.rename(str(D / "a"), str(D / "b"))
        assert (D / "b").read_text() == "a" and not (D / "a").exists()
    (D / "t" / "rloop").symlink_to(D / "t")  # a host tree back down, with a link loop, a dangling link, a pipe
    (D / "t" / "rgone").symlink_to(d / "nowhere")
    os.mkfifo(D / "t" / "rpipe")
    fs.copy(R, str(D / "t"), Lo, str(d / "back"), ticker())
    assert (d / "back" / "big.bin").read_bytes() == big and not (d / "back" / "rloop").exists()
    assert not (d / "back" / "rgone").exists() and not (d / "back" / "rpipe").exists() and not (D / "t" / "pipe").exists()
    for bad in (lambda: fs.copy(R, str(D / "t" / "rpipe"), Lo, str(d / "p"), ticker()),
                lambda: fs.copy(Lo, str(L / "pipe"), R, str(D / "p"), ticker())):
        try:
            bad()
            raise AssertionError("a pipe was opened")
        except OSError as e:
            assert "not a regular file" in str(e)
    me, st = pwd.getpwuid(os.getuid()).pw_name, (D / "b").stat()
    for f in (R, Lo):  # attributes: mode and owner (ids, and the names a listing shows); the modified time
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
    tick.finished = set()
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
(d / "junk.zip").write_bytes(b"not an archive")
try:
    fs.Archive(str(d / "junk.zip"), "junk.zip")
    raise AssertionError("junk opened")
except OSError as e:
    assert "not an archive" in str(e)

assert screen.bar(" /a/long/path", " keys ", 14) == "…g/path keys " and screen.bar(" t", " k ", 10) == " t     k "
# layout: side by side; stacked when the terminal stands upright (a cell is about twice as tall as wide);
# one pane when two don't fit; nothing below the least size
side, stack = ui.layout(30, 120), ui.layout(40, 60)
assert [r[2] for r in side[0]] == [0, 60] and [r[1] for r in stack[0]] == [0, 17] and side[1] == stack[1] == 3
assert ui.layout(8, 44)[0] == [(None, 0, 0, 5, 44)] and ui.layout(5, 80) is None and ui.layout(30, 19) is None
assert ui.layout(10, 100)[0][1][2] == 50 and ui.layout(12, 30)[0][1][1] == 4 and len(ui.layout(7, 30)[0]) == 1  # each falls back to the other, then to one
# viewer: hex rows, wrap, search (forward, backward, wrapping round)
assert view.hexrow(b"AB\x00" + bytes(range(20)), 0).startswith("00000000  41 42 00 00 01 02 03 04  05 06")
assert view.hexrow(b"AB\x00", 0).endswith("|AB.|") and view.hexrow(bytes(17), 1).startswith("00000010  00 ")
assert view.rows(["abcdef", ""], 4, True) == ["abcd", "ef", ""] and view.rows(["abcdef"], 4, False) == ["abcdef"]
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

shutil.rmtree(d)
print("ok")
