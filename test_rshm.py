#!/usr/bin/env python3
import curses, os, shutil, subprocess, tempfile, tomllib, rshm
from pathlib import Path

# config: a host is an ssh destination; a table is a group; a list is a group of destinations named as themselves
h = rshm.tree(tomllib.loads("""
[hosts]
solo = "me@solo"
lab = ["pi", "box"]
[hosts.prod]
web = "deploy@web.example.com"
db = { host = "db", path = "/srv/db" }
""")["hosts"])
assert rshm.tree(tomllib.loads(rshm.SAMPLE)["hosts"]) == {"localhost": {"host": "localhost"}}
rows = list(rshm.flat(h, set()))
names = lambda rs: [r[1] for r in rs]
assert names(rows) == ["solo", "lab", "pi", "box", "prod", "web", "db"]
assert h["lab"] == {"pi": {"host": "pi"}, "box": {"host": "box"}} and h["prod"]["db"]["path"] == "/srv/db"
assert names(rshm.flat(h, {("lab",)})) == ["solo", "lab", "prod", "web", "db"]
assert rshm.tree({"web": 5, "g": {"x": []}, "l": ["ok", 3]}) == {"web": {}, "g": {"x": {}}, "l": {"ok": {"host": "ok"}}}  # no crash
H = {r[1]: r[2] for r in rows if r[2]}

# filter: group name, host name or destination; "" keeps all
assert names(r for r in rows if rshm.match(r, "")) == names(rows)
assert names(r for r in rows if rshm.match(r, "deploy@")) == ["web"]

# rshm HOST: exact name, group/name, then unique substring
paths = lambda q, **k: [p for p, _ in rshm.find(h, q, **k)]
assert paths("web") == ["prod/web"] and paths("lab/pi") == ["lab/pi"] and paths("DB") == ["prod/db"]
assert paths("lab") == ["lab/pi", "lab/box"] and paths("nope") == [] and paths("we", fuzzy=False) == []

# ssh argv: ssh config does user/key/jump; rshm adds only its master and timeouts, exactly once (ssh keeps the first)
a = rshm.argv(H["web"], batch=True, timeout=3)
assert a[0] == "ssh" and "BatchMode=yes" in a and [o for o in a if o.startswith("ConnectTimeout")] == ["ConnectTimeout=3"]
assert not [o for o in a if o.startswith(("User", "Port", "IdentityFile", "ProxyCommand"))]
assert rshm.sshcmd(H["web"], d="/s rv")[-3:] == ["-t", "deploy@web.example.com", "cd '/s rv'; exec \"${SHELL:-sh}\" -l"]
# one master per destination, its socket path short enough for a unix socket
cp = lambda x: next(o for o in rshm.argv(x) if o.startswith("ControlPath="))
assert cp(H["web"]) != cp(H["db"]) and cp(H["web"]) == cp({"host": "deploy@web.example.com"})
assert len(cp(H["web"])) - len("ControlPath=") - len(str(Path.home())) == len("/.ssh/rshm-") + 16

# network failures (retry, don't prompt) vs refusals (a terminal login may help), as OpenSSH words them
for e in ("ssh: connect to host x port 22: Connection refused", "Connection to 127.0.0.1 port 2400 timed out",
          "kex_exchange_identification: Connection closed by remote host", "Connection closed by UNKNOWN port 65535",
          "ssh: Could not resolve hostname nope: Name or service not known", "read: Connection reset by peer"):
    assert rshm.unreachable(e), e
for e in ("bob@x: Permission denied (publickey,password).", "Host key verification failed."):
    assert not rshm.unreachable(e), e

# config file: created with the sample the first time, read back as is
d = Path(tempfile.mkdtemp())
assert rshm.read(d / "new" / "rshm.toml")["theme"] == "default" and (d / "new" / "rshm.toml").read_text() == rshm.SAMPLE

# config save: atomic, 600, and a symlinked config (dotfile repo) stays a symlink
real = d / "dotfiles" / "rshm.toml"
real.parent.mkdir()
real.write_text("old")
(d / "link.toml").symlink_to(real)
rshm.save(d / "link.toml", "theme = 'x'\n")
assert (d / "link.toml").is_symlink() and real.read_text() == "theme = 'x'\n" and real.stat().st_mode & 0o777 == 0o600
assert not list(real.parent.glob(".*rshm-tmp"))
replace, os.replace = os.replace, lambda *_: (_ for _ in ()).throw(OSError("disk full"))
try:
    rshm.save(real, "never")
    raise AssertionError("save didn't fail")
except OSError:
    assert real.read_text() == "theme = 'x'\n" and not list(real.parent.glob(".*rshm-tmp"))  # old file intact, no litter
finally:
    os.replace = replace

# the local folder vanishing underneath us: fall back to home, never crash
gone = Path(tempfile.mkdtemp())
os.chdir(gone)
gone.rmdir()
assert rshm.lcwd() == str(Path.home())

# theme save: only the value changes, comment kept, either quote style, added when missing
for src in ('theme = "amber"   # c\n[hosts]', "theme='amber' # c\n[hosts]", "[hosts]"):
    out = rshm.retheme(src, "matrix")
    assert tomllib.loads(out)["theme"] == "matrix" and out.count("theme") == 1 and ("# c" in out) == ("# c" in src)

# ssh config import: aliases (several per line, key=value, quotes), Includes (relative, glob, cycles), no patterns
(d / "conf.d").mkdir()
(d / "config").write_text('Host web db\n  HostName x\nhost=jump\nHost *.corp !bad "q a"\nInclude conf.d/*.conf config\n')
(d / "conf.d" / "a.conf").write_text("Host inner\n  ProxyJump jump\nMatch host foo\nHost web\nInclude b.conf\n")
(d / "b.conf").write_text("Host nested\n")  # a relative Include is relative to ~/.ssh, wherever it appears
rshm.SSH_CONFIG = d / "config"
assert rshm.ssh_hosts(d / "config") == ["web", "db", "jump", "q a", "inner", "nested"]
(d / "b.conf").unlink()  # live group: aliases not already listed in rshm's own config
lh = rshm.hosts({"hosts": {"mine": "web", "g": ["jump"]}})
assert lh["~/.ssh/config"] == {"db": {"host": "db"}, "q a": {"host": "q a"}, "inner": {"host": "inner"}}
assert [p for p, _ in rshm.find(lh, "inner")] == ["~/.ssh/config/inner"]
rshm.SSH_CONFIG = d / "missing"
assert rshm.hosts({}) == {} and rshm.hosts({"hosts": "junk"}) == {}

# SFTP wire format: raw (non-UTF-8) names round-trip; attrs skip what they don't use
raw = b"caf\xe9\n"
name = raw.decode(errors="surrogateescape")
assert rshm.pack(7, name) == b"\x00\x00\x00\x07\x00\x00\x00\x05" + raw
r = rshm.Reader(rshm.pack(0x80000000 | 0xF) + (1536).to_bytes(8, "big") + rshm.pack(1, 2, 0o40755, 3, 4, 1, "k", "v", 9))
assert r.attrs() == {"size": 1536, "perm": 0o40755} and r.u32() == 9 and rshm.isdir({"perm": 0o40755})
assert rshm.islink({"perm": 0o120777}) and not rshm.isdir({"perm": 0o120777})

# $EDITOR/$PAGER with flags; unset or empty falls back to the classic instead of running the file
os.environ["EDITOR"], os.environ["PAGER"] = "code -w", ""
assert rshm.editor("EDITOR") == ["code", "-w"] and rshm.editor("PAGER") == ["less"]
del os.environ["EDITOR"]
assert rshm.editor("EDITOR") == ["vi"]

# display: keys, sizes, and hostile names can't drive the terminal
assert rshm.codes("x Del") == [ord("x"), curses.KEY_DC] and 10 in rshm.codes("⏎") and rshm.codes("m ?") == [109, 63]
assert [rshm.human(n) for n in (None, 0, 1023, 1536, 5 * 1024 ** 5)] == ["", "0B", "1023B", "1.5K", "5120.0T"]
assert rshm.safe("a\x1b[2Jb\nc" + name) == "a?[2Jb?ccaf??"

# one key table: every action exists, no key is bound twice in a pane
for pane in ("hosts", "files"):
    bound = [c for keys, _, m in rshm.KEYS[pane] + rshm.KEYS["both"] if m for c in rshm.codes(keys)]
    assert len(bound) == len(set(bound)), pane
assert all(hasattr(rshm.App, m) for rows in rshm.KEYS.values() for _, _, m in rows if m)
assert "download" in rshm.keys_help() and "x Del" in rshm.keys_help()

# the tree starts folded: every group, nested ones too; hosts outside any group stay visible
G = rshm.tree({"top": "me@top", "g": {"x": "me@x", "sub": {"y": "me@y"}}})
assert rshm.groups(G) == {("g",), ("g", "sub")}
assert names(rshm.flat(G, rshm.groups(G))) == ["top", "g"]

# each pane filters on its own: a hosts filter never hides files, and the other way round
app = rshm.App.__new__(rshm.App)
app.hosts, app.closed, app.files, app.flt = G, rshm.groups(G), ["../", "x.log", "y.txt"], ["y", ""]
app.sync()
assert [r[1] for r in app.rows] == ["y"] and app.shown == ["../", "x.log", "y.txt"]
app.flt = ["", "log"]
app.sync()
assert [r[1] for r in app.rows] == ["top", "g"] and app.shown == ["../", "x.log"]

# string hosts become tables once, so per-host state (the last folder) survives a refresh
hs = rshm.hosts({"hosts": {"g": {"s": "me@x"}}})
a, b = [h for *_, h, _ in rshm.flat(hs, set()) if h], [h for *_, h, _ in rshm.flat(hs, set()) if h]
assert a[0] is b[0] and a[0] == {"host": "me@x"}

# SFTP transfers against a real sftp-server (the binary OpenSSH runs server-side), no network needed
srv = next((x for x in (shutil.which("sftp-server"), "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server",
                        "/usr/libexec/sftp-server") if x and os.path.exists(x)), None)
if srv:
    s = rshm.SFTP.__new__(rshm.SFTP)
    s.err, s.n, s.early, s.ext = tempfile.TemporaryFile(), 0, {}, set()
    s.p = subprocess.Popen([srv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=s.err)
    s.init()
    L, R = d / "local", d / "remote"
    (L / "sub").mkdir(parents=True)
    R.mkdir()
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
    rshm.send(s, str(L), str(R / "t"), seen)
    assert (R / "t" / "big.bin").read_bytes() == big and (R / "t" / "sub" / "x.txt").read_text() == "x"
    assert seen.done == len(big) + 1 and not (R / "t" / "loop").exists() and not (R / "t" / "gone").exists()
    rshm.send(s, str(L), str(R / "t"), seen)  # a reconnect re-walks the tree: finished files aren't sent again
    assert seen.done == len(big) + 1 and len(seen.finished) == 2
    assert not list(R.rglob("*" + rshm.PART))  # every file renamed into place
    # replacing keeps permissions; download resumes a partial and restarts a bogus one
    os.chmod(R / "t" / "sub" / "x.txt", 0o600)
    (L / "sub" / "x.txt").write_text("new")
    rshm.send(s, str(L / "sub" / "x.txt"), str(R / "t" / "sub" / "x.txt"), ticker())
    assert (R / "t" / "sub" / "x.txt").read_text() == "new" and (R / "t" / "sub" / "x.txt").stat().st_mode & 0o777 == 0o600
    got = ticker()
    (d / ("dl" + rshm.PART)).write_bytes(big[:1_000_000])
    rshm.fetch(s, str(R / "t" / "big.bin"), str(d / "dl"), got)
    assert (d / "dl").read_bytes() == big and got.log[0][1:] == (1_000_000, True) and got.done == len(big)
    (d / ("dl" + rshm.PART)).write_bytes(big[:3_000_000])  # a retry of the same file: resumed bytes are never counted twice
    got.finished.clear()
    rshm.fetch(s, str(R / "t" / "big.bin"), str(d / "dl"), got)
    assert got.done == len(big)
    (d / ("dl2" + rshm.PART)).write_bytes(b"z" * (len(big) + 5))
    rshm.fetch(s, str(R / "t" / "big.bin"), str(d / "dl2"), ticker())
    assert (d / "dl2").read_bytes() == big
    # upload resumes a partial .part; a remote .part that isn't a prefix length restarts
    (R / "t" / ("up.bin" + rshm.PART)).write_bytes(big[:2_000_000])
    rshm.send(s, str(L / "big.bin"), str(R / "t" / "up.bin"), ticker())
    assert (R / "t" / "up.bin").read_bytes() == big and b"posix-rename@openssh.com" in s.ext
    # downloads keep the remote permissions (owner-writable); an upload onto a symlink writes through it
    os.chmod(R / "t" / "sub" / "x.txt", 0o750)
    rshm.fetch(s, str(R / "t" / "sub" / "x.txt"), str(d / "perm"), ticker())
    assert (d / "perm").stat().st_mode & 0o777 == 0o750 & ~os.umask(os.umask(0))
    (R / "t" / "ln").symlink_to("sub/x.txt")
    (L / "sub" / "x.txt").write_text("thru")
    rshm.send(s, str(L / "sub" / "x.txt"), str(R / "t" / "ln"), ticker())
    assert (R / "t" / "ln").is_symlink() and (R / "t" / "sub" / "x.txt").read_text() == "thru"
    assert (R / "t" / "sub" / "x.txt").stat().st_mode & 0o777 == 0o750
    # a stray user file named like a partial is not ours: never spliced in, never removed
    (R / "t" / "x.part").write_text("MYDATA")
    rshm.send(s, str(L / "sub" / "x.txt"), str(R / "t" / "x"), ticker())
    assert (R / "t" / "x").read_text() == "thru" and (R / "t" / "x.part").read_text() == "MYDATA"
    # a recursive delete
    rshm.send(s, str(L / "sub"), str(R / "t" / "del"), ticker())
    s.rm(str(R / "t" / "del"))
    assert not (R / "t" / "del").exists()
    # remote tree back down, with a remote link loop in it
    (R / "t" / "rloop").symlink_to(R / "t")
    (R / "t" / "rgone").symlink_to(d / "nowhere")
    os.mkfifo(R / "t" / "rpipe")
    rshm.fetch(s, str(R / "t"), str(d / "back"), ticker())
    assert (d / "back" / "big.bin").read_bytes() == big and not (d / "back" / "rloop").exists() and not (d / "back" / "rgone").exists()
    assert not (d / "back" / "rpipe").exists() and not (R / "t" / "pipe").exists()
    for bad in (lambda: rshm.fetch(s, str(R / "t" / "rpipe"), str(d / "p"), ticker()),
                lambda: rshm.send(s, str(L / "pipe"), str(R / "p"), ticker())):
        try:
            bad()
            raise AssertionError("a pipe was opened")
        except OSError as e:
            assert "not a regular file" in str(e)
    s.close()
else:
    print("(no sftp-server here: transfer tests skipped)")

print("ok")
