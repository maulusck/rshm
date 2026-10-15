#!/usr/bin/env python3
"""The hard proof, seeded so any failure replays (RSHM_SEED=n): random trees copied every way and held against
cp -a; text, editor, viewer, wire and config fuzzed; and faults: the link dropped before any reply, ^C before any
reply, a full disk, a source that shrinks mid-copy. Against a real sftp-server, no network, no sshd."""
import hashlib, os, random, shutil, stat, subprocess, sys, tempfile
from pathlib import Path

from rshm import config, edit, fs, screen, sftp, ui, view

SEED = int(os.environ.get("RSHM_SEED", 1))
rnd = random.Random(SEED)
SRV = next((x for x in (shutil.which("sftp-server"), "/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server",
                        "/usr/libexec/sftp-server") if x and os.path.exists(x)), None)
d = Path(tempfile.mkdtemp(prefix="rshm-stress-"))
print(f"seed {SEED}")

# --- text in cells: any string, any width, keep: exactly that wide, nothing raw reaches the terminal ---
ALPHA = "ab Z~日本é́\t\x1b\x07​😀\x00\x7f﻿\udcff…"
for _ in range(3000):
    s = "".join(rnd.choice(ALPHA) for _ in range(rnd.randrange(40)))
    for w in range(0, 14):
        for keep in ("end", "mid"):
            f = screen.fit(s, w, keep)
            assert screen.cells(f) == w and f.isprintable(), (s, w, keep, f)
        assert screen.cells(screen.span(s, rnd.randrange(30), w)) == w
        assert screen.cells(screen.bar(s, s[::-1], w + 1)) == w, (s, w)  # a status line never runs off the screen
        rows = screen.wrap(s, w)
        assert "".join(rows) == s and all(screen.cells(r) <= max(w, 2) for r in rows), (s, w, rows)

# --- the viewer's text: any bytes, nothing raw; search finds what a plain scan finds ---
for _ in range(2000):
    data = bytes(rnd.choice(b"ab\r\n\t\x00\x1b\xff\xc3\xa9") for _ in range(rnd.randrange(60)))
    lines = view.text(data)
    assert lines and all(ln.isprintable() for ln in lines), data
    assert len(lines) == data.count(b"\n") + (not data.endswith(b"\n")) or data == b"" and lines == [""], data
    q, start, step = rnd.choice("ab?"), rnd.randrange(len(lines)), rnd.choice((1, -1))
    hits = [i % len(lines) for i in range(start, start + step * len(lines), step) if q in lines[i % len(lines)].lower()]
    assert view.find(len(lines), lambda i: lines[i], q, start, step) == (hits[0] if hits else None)

# --- the editor: any bytes come back exactly; any edits, all undone, give the text back ---
for _ in range(1500):
    data = bytes(rnd.choice(b"ab\r\n\t\xff\xe6\x97\xa5") for _ in range(rnd.randrange(50)))
    b = edit.Buffer(data)
    assert b.data() == data
    first = list(b.lines)
    for _ in range(rnd.randrange(60)):
        op = rnd.randrange(10)
        if op == 0:
            b.insert(rnd.choice(["x", "日", "\t", "é"]))
        elif op == 1:
            b.newline()
        elif op == 2:
            b.backspace()
        elif op == 3:
            b.delete()
        elif op == 4:
            b.cut()
        elif op == 5:
            b.paste()
        elif op == 6:
            b.replace(rnd.choice("ab"), rnd.choice(["", "B", "ab"]))
        elif op == 7:
            b.move(rnd.randrange(-3, 4), rnd.randrange(-3, 4))
        elif op == 8:
            b.goto(rnd.randrange(-2, len(b.lines) + 2), rnd.choice([None, rnd.randrange(20)]))
        else:
            b.find(rnd.choice("ab"))
        assert b.lines and 0 <= b.y < len(b.lines) and 0 <= b.x <= len(b.lines[b.y]), (data, op, b.y, b.x)
    while b.undos:
        b.undo()
    assert b.lines == first, data

# --- the wire: names of any bytes and every attribute round-trip; a broken packet is an OSError, never a hang ---
for _ in range(2000):
    name = bytes(rnd.randrange(256) for _ in range(rnd.randrange(20)))
    ch = {k: rnd.randrange(1 << 32) for k in rnd.sample(["uid", "perm", "mtime"], rnd.randrange(4))}
    ch |= {"gid": rnd.randrange(1 << 32)} if "uid" in ch else {}
    r = sftp.Reader(sftp.pack(name.decode(errors="surrogateescape"), *sftp.packattrs(ch)))
    assert r.str() == name and r.attrs() == ch
    cut = r.d[:rnd.randrange(len(r.d))]
    try:
        sftp.Reader(cut).str(), sftp.Reader(cut[4 + len(name):]).attrs()
    except OSError:
        pass

# --- config: any TOML at all gives hosts whose fields are what they should be, or none ---
def anything(depth=0):  # any TOML value: text, number, flag, list, table, a host-ish table
    k = rnd.randrange(6 if depth < 3 else 3)
    if k == 3:
        return [anything(depth + 1) for _ in range(rnd.randrange(3))]
    if k == 4:
        return {rnd.choice(["host", "path", "x", "g"]) + rnd.choice(["", "", "2"]): anything(depth + 1)
                for _ in range(rnd.randrange(4))}
    if k == 5:
        return {"host": anything(depth + 1), "path": anything(depth + 1)}
    return [rnd.choice(["s", "", "a b"]), rnd.randrange(9), True][k]


for _ in range(3000):
    c = {"hosts": anything(), "include": anything(), "commands": anything(), "open": anything(), "tools": anything()}
    hs = config.hosts(c)
    for _, name, h, path in config.flat(hs, set()):
        assert isinstance(name, str) and (h is None or isinstance(h["host"], str) and isinstance(h.get("path", ""), str))
        assert config.match((0, name, h, path), "a") in (True, False)
    for n, cmd in config.table(c, "commands").items():
        assert isinstance(cmd, str)
    assert config.opener(c, "x.y") is None or isinstance(config.opener(c, "x.y"), str)
    assert config.tool(c, "edit") is None or isinstance(config.tool(c, "edit"), str)

if not SRV:
    sys.exit("(no sftp-server here: the copy proofs skipped) stress ok")

# --- a host, as rshm reaches one: its sessions are a real sftp-server, started the way ssh would start it ---
LIMIT, BANNER, DENY = [], [], []  # a file size limit (a full disk); what a login shell prints; requests it refuses
sftp.argv = lambda h, batch=False, timeout=8: [
    sys.executable, "-c", "import os, resource, signal, sys\nif sys.argv[2] != '-':\n signal.signal(signal.SIGXFSZ, "
    "signal.SIG_IGN)\n resource.setrlimit(resource.RLIMIT_FSIZE, (int(sys.argv[2]),) * 2)\nos.write(1, sys.argv[3]."
    "encode())\nos.execv(sys.argv[1], [sys.argv[1], *(['-P', sys.argv[4]] if sys.argv[4] != '-' else [])])", SRV,
    str(LIMIT[0]) if LIMIT else "-", "".join(BANNER), ",".join(DENY) or "-"]
fs.connect = lambda *a: None  # the login is ssh's business: here, nothing to log in to
FAULT = {"n": 0, "at": set(), "do": None, "hit": 0}  # before reply number n: drop the link, or ^C
recv = sftp.SFTP.recv


def faulty(s):
    FAULT["n"] += 1
    if FAULT["n"] in FAULT["at"]:
        FAULT["hit"] += 1
        if FAULT["do"] == "drop":  # what was on its way is lost with it: the fault lands at exactly this reply
            s.p.kill()
            s.p.stdout.read()
        else:
            raise KeyboardInterrupt
    return recv(s)


sftp.SFTP.recv = faulty
Lo = fs.Local()


def remote(copy_data=True):  # a host; without copy-data, copies within it go through a second session
    r = fs.Remote({"host": "test"}, None, None)
    if not copy_data:
        r.sftp().ext.discard(b"copy-data")
    return r


def ticker(on=None):  # the App's ticker contract; on(path, at) runs at each report
    def tick(path, at, resumed=False):
        tick.done, tick.pos[path] = tick.done + at - tick.pos.get(path, 0), at
        on and on(path, at)
    tick.pos, tick.done, tick.finished, tick.skipped = {}, 0, set(), []
    return tick


def sig(root, top=True):  # everything cp -a keeps that rshm promises to: kind, mode, times (not links'), content, target
    out = {}
    for p in [Path(root), *Path(root).rglob("*")] if top else Path(root).rglob("*"):
        st, rel = p.lstat(), str(p.relative_to(root))
        if stat.S_ISLNK(st.st_mode):
            out[rel] = ("link", os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            out[rel] = ("dir", stat.S_IMODE(st.st_mode), int(st.st_mtime))
        elif stat.S_ISREG(st.st_mode):
            out[rel] = ("file", stat.S_IMODE(st.st_mode) & 0o777, int(st.st_mtime), hashlib.sha1(p.read_bytes()).hexdigest())
    return out


NAMES = ["a", "B c", "-x", "ünï", "日本", "é", "😀", "x\udcff", "t\tb", "n\nl", " sp", ".hid", "'q\"", "$(x)", "*",
         "x.part", "z" * 200]
SIZES = [0, 1, 100, sftp.CHUNK - 1, sftp.CHUNK, sftp.CHUNK + 1, 3 * sftp.CHUNK + 7]
MODES = [0o644, 0o600, 0o755, 0o400, 0o000, 0o4755, 0o777, 0o640]


def tree(root, depth=0, files=None):  # a random tree: every kind of entry and name, odd sizes and modes, fixed times
    files = [] if files is None else files
    os.mkdir(root)
    for i in range(rnd.randrange(1, 8)):
        p, k = os.path.join(root, f"{rnd.choice(NAMES)}{i}"), rnd.random()
        if k < .2 and depth < 3:
            tree(p, depth + 1, files)
        elif k < .3:
            os.symlink(rnd.choice(["..", "nowhere", "/", os.path.basename(p), "a0", os.path.abspath(root)]), p)
        elif k < .34:
            os.mkfifo(p)
        elif k < .38 and files:
            os.link(rnd.choice(files), p)
        else:
            n = rnd.choice(SIZES) if rnd.random() < .8 else rnd.randrange(sftp.WINDOW * sftp.CHUNK + 99)
            Path(p).write_bytes(rnd.randbytes(n))
            os.chmod(p, rnd.choice(MODES))
            os.utime(p, (t := rnd.randrange(1 << 31), t))
            files.append(p)
    os.chmod(root, rnd.choice([0o755, 0o700, 0o555, 0o775]))
    os.utime(root, (t := rnd.randrange(1 << 31), t))
    return files


def wipe(p):
    if os.path.lexists(p):
        subprocess.run(["chmod", "-R", "u+rwx", p], stderr=subprocess.DEVNULL)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)


def size(root):
    return sum(p.lstat().st_size for p in Path(root).rglob("*") if p.is_file() and not p.is_symlink())


# --- differential: random trees copied every way, each the same as cp -a makes it ---
R, R2 = remote(), remote(False)
kinds = {"file": 0, "dir": 0, "link": 0, "pipe": 0, "odd name": 0}
for rnd_tree in range(int(os.environ.get("RSHM_TREES", 6))):
    src, ref = d / "src", d / "ref"
    tree(src)
    subprocess.run(["cp", "-a", src, ref], check=True)
    want = sig(ref)  # cp -a copies pipes too; rshm never opens one (a tree's pipes are reported, not copied)
    pipes = {str(p) for p in src.rglob("*") if stat.S_ISFIFO(p.lstat().st_mode)}
    for v in want.values():
        kinds[v[0]] += 1
    kinds["pipe"] += len(pipes)
    kinds["odd name"] += sum(not k.isascii() or not k.isprintable() for k in want)
    tar = d / "t.tgz"
    with __import__("tarfile").open(tar, "w:gz") as tf:
        tf.add(src, arcname=".")
    ar = fs.Archive(str(tar), "t.tgz")
    for name, a, b in (("local", Lo, Lo), ("up", Lo, R), ("down", R, Lo), ("server copy", R, R), ("twin", R2, R2),
                       ("archive", ar, Lo)):
        out = d / "out"
        sp = "/" if a is ar else str(src)
        t = ticker()
        fs.copy(a, sp, b, str(out), t)
        top = a is not ar  # an archive's top has no mode or time of its own
        got, exp = sig(out, top), sig(ref, top)
        assert got == exp, (rnd_tree, name, [(k, got.get(k), exp.get(k)) for k in {*got, *exp} if got.get(k) != exp.get(k)][:3])
        assert t.done == size(ref), (name, t.done, size(ref))
        assert set(t.skipped) == pipes or not top, (name, t.skipped)
        fs.copy(a, sp, b, str(out), ticker())  # again, onto itself: the same tree, nothing doubled
        assert sig(out, top) == got and not list(out.rglob("*" + fs.PART)), name
        wipe(out)
    ar.close()
    for p in (src, ref, tar):
        wipe(p)
print(f"differential ok ({kinds})")

# --- the link drops before reply n, any n, again and again: the transfer reconnects, resumes, ends exact ---
class App:  # what App.xfer needs of the app around it
    msg, busy, ticker, xfer = "", lambda s, what: None, ui.App.ticker, ui.App.xfer


ui.time.sleep = lambda n: None
src = d / "src"
tree(src)
os.chmod(src, 0o755)
blob = rnd.randbytes(sftp.WINDOW * sftp.CHUNK * 2 + 5)
(src / "blob").write_bytes(blob)
want = sig(src)
for name, mk in (("up", lambda: (Lo, remote())), ("down", lambda: (remote(), Lo)),
                 ("server copy", lambda: (r := remote(), r)), ("twin", lambda: (r := remote(False), r))):
    FAULT["at"], FAULT["n"] = set(), 0
    a, b = mk()
    fs.copy(a, str(src), b, str(d / "out"), ticker())
    clean = FAULT["n"]  # the replies a whole copy takes
    a.close(), b.close()
    wipe(d / "out")
    for run in range(12):
        a, b = mk()
        FAULT["n"], FAULT["do"] = 0, "drop"
        FAULT["at"] = set(rnd.sample(range(2, clean), min(rnd.randrange(1, 5), clean - 2)))
        app = App()
        app.xfer(lambda t: fs.copy(a, str(src), b, str(d / "out"), t), name, None, [a, b])
        assert sig(d / "out") == want and not list((d / "out").rglob("*" + fs.PART)), (name, run, FAULT)
        assert app.msg.startswith("✓"), app.msg
        wipe(d / "out")
        a.close(), b.close()
FAULT["at"] = set()
assert FAULT["hit"] > 40, FAULT  # it did drop, a lot
print(f"dropped links ok ({FAULT['hit']} drops)")

# a link that never comes back: a few tries, then the error, not forever
def refuse(*a):
    raise ConnectionError("ssh: connect to host test port 22: Connection refused")


fs.connect, app = refuse, App()
app.busy = lambda what: app.said.append(what)
app.said, r = [], fs.Remote({"host": "test"}, None, None)
try:
    app.xfer(lambda t: fs.copy(Lo, str(src / "blob"), r, str(d / "x"), t), "x", None, [r])
    raise AssertionError("a dead host succeeded")
except OSError as e:
    assert "refused" in str(e) and sum("reconnecting" in w for w in app.said) == 5, (e, app.said)
fs.connect = lambda *a: None

# a drop before any reply of a lookup: the right answer or an error, never "nothing there"
q = d / "q"
(q / "sub").mkdir(parents=True)
(q / "f").write_bytes(b"f")
(q / "ln").symlink_to("f")
(q / "sub" / "g").write_bytes(b"g")
for name, op in (("peek", lambda r: fs.peek(r, str(q / "ln"))), ("look", lambda r: sorted(fs.look(r, str(q)), key=str)),
                 ("walk", lambda r: sorted(fs.walk(r, str(q)), key=str))):
    FAULT["n"], FAULT["at"] = 0, set()
    r = remote()
    truth, clean = op(r), FAULT["n"]
    r.close()
    for n in range(1, clean + 1):
        r = remote()
        FAULT["n"], FAULT["at"], FAULT["do"] = 0, {n}, "drop"
        try:
            assert op(r) == truth, (name, n)
        except OSError:
            pass
        r.close()
FAULT["at"] = set()

# a source that can't be read: refused before anything is made at the other end
DENY[:] = ["open"]
r = remote()
try:
    fs.copy(r, str(q / "f"), Lo, str(d / "denied"), ticker())
    raise AssertionError("read a refused file")
except OSError:
    assert not list(d.glob("denied*")), list(d.glob("denied*"))
r.close()
DENY[:] = []

# a server that won't set modes or times: the files still arrive, whole
DENY[:] = ["setstat", "fsetstat"]
for a, b in ((Lo, remote()), (r := remote(), r)):
    fs.copy(a, str(q), b, str(d / "noattr"), ticker())
    assert (d / "noattr" / "sub" / "g").read_bytes() == b"g" and (d / "noattr" / "f").read_bytes() == b"f"
    wipe(d / "noattr")
    b.close()
DENY[:] = []

# a request to a session already gone: a lost link at once, the session closed
sf = remote().sftp()
sf.p.kill()
sf.p.wait()
try:
    sf.realpath(".")
    raise AssertionError("a dead session answered")
except ConnectionError:
    assert not sf.alive()

# a login shell that prints before the server starts: said plainly, not a hang or a garbled session
BANNER[:] = ["Welcome to the host!\n"]
try:
    remote().sftp()
    raise AssertionError("a banner went unnoticed")
except OSError as e:
    assert "before SFTP" in str(e) and "Welcome" in str(e), e
BANNER[:] = []

# a server that answers a read with less than asked: the rest is fetched, the bytes are exact
wait, SHORT = sftp.SFTP.wait, [3]


def short(s, i):
    r = wait(s, i)
    if SHORT[0] and r is not None and len(r.d) > 1000:  # a DATA reply: cut short
        SHORT[0] -= 1
        return sftp.Reader(sftp.pack(r.str()[:100]))
    return r


sftp.SFTP.wait = short
r = remote()
assert b"".join(r.read(str(src / "blob"), 0, len(blob))) == blob and not SHORT[0]
r.close()
sftp.SFTP.wait = wait

# --- ^C before reply n, every n: nothing half-done is ever in place; the retry ends exact ---
small = d / "small"
small.mkdir()
(small / "f").write_bytes(rnd.randbytes(3 * sftp.CHUNK + 1))
(small / "g").write_bytes(b"g")
(small / "ln").symlink_to("f")
(small / "sub").mkdir()
(small / "sub" / "h").write_bytes(rnd.randbytes(sftp.WINDOW * sftp.CHUNK + 3))
want = sig(small)
old = b"the old content"
for name, mk in (("up", lambda: (Lo, remote())), ("down", lambda: (remote(), Lo)),
                 ("server copy", lambda: (r := remote(), r)), ("twin", lambda: (r := remote(False), r))):
    FAULT["at"], FAULT["n"] = set(), 0
    a, b = mk()
    fs.copy(a, str(small), b, str(d / "out"), ticker())
    clean = FAULT["n"]
    a.close(), b.close()
    for n in range(1, clean + 1):
        wipe(d / "out")
        (d / "out").mkdir()
        (d / "out" / "f").write_bytes(old)
        a, b = mk()
        FAULT["n"], FAULT["at"], FAULT["do"] = 0, {n}, "int"
        try:
            fs.copy(a, str(small), b, str(d / "out"), ticker())
        except KeyboardInterrupt:
            pass
        assert (d / "out" / "f").read_bytes() in (old, (small / "f").read_bytes()), (name, n)  # never half of it
        assert not list((d / "out").rglob("*" + fs.PART)), (name, n)  # stopped: nothing of it left there
        FAULT["at"] = set()
        fs.copy(a, str(small), b, str(d / "out"), ticker())  # you try again
        assert sig(d / "out") == want and not list((d / "out").rglob("*" + fs.PART)), (name, n)
        a.close(), b.close()
    for n in range(1, 40):  # the editor's ^S on a host, ^C at any reply: the old file or the new, never half
        r = remote()
        (d / "saved").write_bytes(old)
        FAULT["n"], FAULT["at"] = 0, {n}
        try:
            fs.save(r, str(d / "saved"), blob)
        except KeyboardInterrupt:
            pass
        FAULT["at"] = set()
        assert (d / "saved").read_bytes() in (old, blob), n
        r.close()
assert FAULT["hit"] > 300, FAULT
print(f"^C ok ({FAULT['hit']} faults in all)")

# --- a full disk (a file size limit on the server; on this machine's side, in a child): an error, the target untouched,
# the part kept to resume from once there's room ---
(d / "out").mkdir(exist_ok=True)
(d / "out" / "blob").write_bytes(old)
LIMIT[:] = [1 << 20]
r = remote()
try:
    App().xfer(lambda t: fs.copy(Lo, str(src / "blob"), r, str(d / "out" / "blob"), t), "full", None, [r])
    raise AssertionError("a full disk took the file")
except OSError as e:
    assert r.alive() and ("Failure" in str(e) or "large" in str(e)), e
assert (d / "out" / "blob").read_bytes() == old and (d / "out" / ("blob" + fs.PART)).stat().st_size <= 1 << 20
r.close()
LIMIT[:] = []
t, r = ticker(), remote()
fs.copy(Lo, str(src / "blob"), r, str(d / "out" / "blob"), t)
r.close()
assert (d / "out" / "blob").read_bytes() == blob and t.pos[str(src / "blob")] == len(blob)
(d / "out" / "blob").write_bytes(old)
child = subprocess.run([sys.executable, "-c", f"""
import resource, signal, sys
from rshm import fs
signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
t = lambda *a, **k: None
t.finished, t.skipped = set(), []
try:
    fs.copy(fs.Local(), {str(src / "blob")!r}, fs.Local(), {str(d / "out" / "blob")!r}, t)
except OSError as e:
    sys.exit(f"refused: {{e}}")
"""], capture_output=True, text=True, cwd=Path(__file__).parent)
assert "refused: [Errno 27] File too large" in child.stderr, child.stderr
assert (d / "out" / "blob").read_bytes() == old
print("full disk ok")

# --- a source that shrinks while it's read: an error, never a short file in place ---
for name, a, b in (("up", Lo, remote()), ("down", remote(), Lo), ("local", Lo, Lo)):
    (src / "shrinks").write_bytes(blob)
    (d / "out" / "shrinks").write_bytes(old)
    cut = lambda path, at: at and os.truncate(src / "shrinks", sftp.CHUNK)
    try:
        fs.copy(a, str(src / "shrinks"), b, str(d / "out" / "shrinks"), ticker(cut))
        raise AssertionError(f"{name}: a short copy went through")
    except OSError as e:
        assert "shrank" in str(e), (name, e)
    assert (d / "out" / "shrinks").read_bytes() == old, name
    a.close(), b.close()
print("shrinking source ok")

# --- nothing left behind: every session closed is gone, no stray server, no zombie ---
for r in (R, R2):
    r.close()
def kids():
    out = []
    for p in filter(str.isdigit, os.listdir("/proc")):
        try:
            out += [p] if open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1] == str(os.getpid()) else []
        except OSError:
            pass  # gone meanwhile
    return out


assert not kids(), kids()
wipe(d)
print("stress ok")
