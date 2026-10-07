#!/usr/bin/env python3
"""rshm - Remote SHell Manager. One TOML, one screen. Shells out to OpenSSH: ~/.ssh/config, keys, agent just work."""
import atexit, contextlib, curses, glob, hashlib, os, re, shlex, shutil, signal, stat, struct, subprocess, sys, tempfile, time
import tomllib
from collections import deque
from pathlib import Path

CFG = Path("~/.config/rshm.toml").expanduser()
SSH_CONFIG = Path.home() / ".ssh" / "config"
SAMPLE = """\
theme = "default"   # default | matrix | amber | frutiger | teletext   (`t` cycles and saves)

# A host is an ssh destination: an ~/.ssh/config alias, a host or user@host. Users, keys, jumps and passwords
# belong in ~/.ssh/config. Its hosts show up here on their own; list them to group them.
[hosts]
localhost = "localhost"
# lab = ["pi", "box"]                     # a group of hosts, each named as itself
# [hosts.prod]                            # a table is a group too; nest as deep as you like
# web = "deploy@web.example.com"          # name = destination
# db = { host = "db", path = "/srv/db" }  # path: the folder to start in
"""
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
MOVES = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1, curses.KEY_PPAGE: -10, curses.KEY_NPAGE: 10}
NAMES = {"⏎": (10, 13, curses.KEY_ENTER), "→": (curses.KEY_RIGHT,), "←": (curses.KEY_LEFT,), "Space": (32,),
         "⌫": (curses.KEY_BACKSPACE, 127, 8), "Del": (curses.KEY_DC,), "Tab": (9,), "Esc": (27,), "F2": (curses.KEY_F2,)}
KEYS = {  # the one source for key dispatch, the in-app menu/help, the hint bar and --help: (keys, what, method)
    "hosts": [("⏎", "ssh / fold group", "enter"), ("→ l Space", "browse / unfold", "hopen"),
              ("← h ⌫", "fold / to group", "hclose"), ("p", "ping", "ping"), ("i", "install my key", "copyid"),
              ("e", "edit config", "edit")],
    "files": [("⏎ → l", "open / view", "fopen"), ("← h ⌫", "parent folder", "parent"), ("v", "view copy", "view"),
              ("e", "edit copy", "fedit"), ("d", "download", "get"), ("u", "upload", "put"),
              ("n +", "new folder", "fmkdir"), ("R F2", "rename / move", "frename"), ("x Del", "delete", "fdel"),
              ("s", "shell here", "here"), ("g", "go to path", "goto"), ("~", "home", "home"), ("c", "local folder", "lcd")],
    "both": [("↑ ↓ j k PgUp PgDn", "move", None), ("/", "filter this pane: ⏎ applies, Esc clears", "search"),
             ("Esc", "back / clear filter", "esc"), ("Tab", "switch pane", "tab"), ("t", "next theme", "cycle_theme"),
             ("r", "reload", "reload"), ("m ?", "this menu: all keys", "ctx"), ("q", "quit", "quit"),
             ("click", "select; again: open / ssh", None), ("right-click", "this menu", None), ("wheel", "scroll", None)],
}
SPIN, BIG = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 50 << 20  # spinner frames; view/edit asks before fetching more than BIG bytes
PART = ".rshm-part"  # a file in transit: resumed from, renamed into place once complete


def keys_help():  # `rshm --help` shows the same table the app runs on
    return "\n".join(f"{title}:\n" + "\n".join(f"  {keys:<20} {what}" for keys, what, _ in KEYS[pane])
                     for pane, title in (("hosts", "hosts pane"), ("files", "files pane"), ("both", "both panes")))


# --- config ---
def read(cfg):  # the config, created with a commented sample the first time
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(SAMPLE)
    return tomllib.loads(cfg.read_text())


def save(path, text, mode=0o600):  # atomic (a crash can't leave half a file), private all the way, symlinks kept
    path = path.resolve()
    tmp = path.with_name(f".{path.name}.rshm-tmp")
    tmp.unlink(missing_ok=True)
    try:
        tmp.touch(mode)
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:  # disk full, ^C: no half-written temp file left behind
        tmp.unlink(missing_ok=True)
        raise


def retheme(txt, name):  # swap just the theme value; comments and layout stay
    new, n = re.subn(r"""^(theme\s*=\s*)(["']).*?\2""", rf'\g<1>"{name}"', txt, count=1, flags=re.M)
    return new if n else f'theme = "{name}"\n' + txt


def hosts(c):  # rshm's hosts, plus a live group of ~/.ssh/config aliases not listed here: nothing to import
    hs = tree(c.get("hosts"))
    try:
        known = {h["host"] for *_, h, _ in flat(hs, set()) if h}
        extra = [a for a in ssh_hosts(SSH_CONFIG) if a not in known] if SSH_CONFIG.exists() else []
    except (OSError, ValueError):  # an unreadable ssh config must not take rshm down with it
        extra = []
    return {**hs, "~/.ssh/config": tree(extra)} if extra else hs


def tree(node):  # every host a table {"host": destination, ...}, once, so per-host state (last folder) sticks to it
    if isinstance(node, list):  # a list is a group of destinations, each named as itself
        node = {x: x for x in node if isinstance(x, str)}
    out = {}
    for k, v in (node.items() if isinstance(node, dict) else ()):
        out[k] = {"host": v} if isinstance(v, str) else v if isinstance(v, dict) and "host" in v else tree(v)
    return out


def ssh_hosts(path, seen=()):  # Host aliases of an ssh config, Includes followed, patterns skipped
    out = []
    for line in path.read_text().splitlines():
        m = re.match(r"(\w+)\s*[=\s]\s*(.+)", line.strip())
        k, v = (m[1].lower(), m[2]) if m else ("", "")
        if k == "include" and path not in seen:
            for pat in shlex.split(v):
                for f in sorted(glob.glob(str(SSH_CONFIG.parent / Path(pat).expanduser()))):
                    out += ssh_hosts(Path(f), (*seen, path))
        elif k == "host":
            out += [a for a in shlex.split(v) if not any(c in a for c in "*?!")]
    return list(dict.fromkeys(out))


def flat(node, closed, depth=0, path=()):  # the tree (from tree()) as rows: (depth, name, host or None, path)
    for k, v in node.items():
        p = path + (k,)
        if "host" in v:
            yield depth, k, v, p
        else:
            yield depth, k, None, p
            if p not in closed:
                yield from flat(v, closed, depth + 1, p)


def groups(hosts):  # every group's path: the tree starts with all of them folded
    return {p for _, _, h, p in flat(hosts, set()) if not h}


def match(row, q):  # q already lowercase; "" matches all
    return any(q in x.lower() for x in row[3]) or bool(row[2]) and q in row[2]["host"].lower()


def find(hosts, name, fuzzy=True):  # exact name or group/name, else (fuzzy) unique substring: `rshm we` -> prod/web
    hs = [("/".join(p), h) for _, k, h, p in flat(hosts, set()) if h]
    exact = [x for x in hs if name in (x[0], x[0].rsplit("/", 1)[-1])]
    return exact if exact or not fuzzy else [x for x in hs if name.lower() in x[0].lower()]


# --- ssh: everything about reaching a host (user, key, jump, password) is ~/.ssh/config's business ---
def argv(h, batch=False, timeout=8):  # ssh keeps the FIRST value of an option: these win over ~/.ssh/config
    o = {"ControlMaster": "auto", "ControlPersist": 300, "ConnectTimeout": timeout,
         "ServerAliveInterval": 15,  # a dead link errors out in ~45 s, never hangs
         # one logged-in master per destination, reused by everything after; a short fixed name, not %C: a unix
         # socket path must fit in ~104 bytes, even under Termux's long $HOME with ssh's own suffix added
         "ControlPath": Path.home() / ".ssh" / f"rshm-{hashlib.sha1(h['host'].encode()).hexdigest()[:16]}"}
    if batch:
        o["BatchMode"] = "yes"
    return ["ssh", *(x for k, v in o.items() for x in ("-o", f"{k}={v}"))]


def sshcmd(h, d=""):  # ponytail: `d` needs a POSIX login shell; plain ssh (no d) works on any host
    return [*argv(h), "-t", h["host"], f'cd {shlex.quote(d)}; exec "${{SHELL:-sh}}" -l'] if d else [*argv(h), h["host"]]


def last(f):  # the last line written to a stderr file: what ssh had to say
    f.seek(0)
    lines = f.read().decode(errors="replace").strip().splitlines()
    return lines[-1] if lines else ""


def quiet(cmd):  # run detached from any terminal -> (exit code, last stderr line)
    with tempfile.TemporaryFile() as err:  # a file, not a pipe: a master forking off with it can't hang us
        # own session: masters and their jump tunnels outlive whatever terminal rshm runs in
        rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                            start_new_session=True).returncode
        return rc, last(err) or f"exit {rc}"


def unreachable(err):  # the network failed (retry later), as opposed to the server refusing us (typing may help)
    return bool(re.search(r"^ssh: |timed out|refused|reset by|[Cc]onnection closed|No route|resolve|unreachable", err))


LOGINS = []  # the masters this rshm started: stopped when it exits, so no ssh outlives it


def logout():  # -O stop, not exit: a master still carrying a session (another rshm's) lets it finish, then quits
    for h in reversed(LOGINS):
        quiet([*argv(h), "-O", "stop", h["host"]])


def connect(h, term, timeout=8, say=lambda m: print(m, file=sys.stderr)):  # make sure h's master is up, logged in
    if quiet([*argv(h), "-O", "check", h["host"]])[0] == 0:
        return
    say(f"connecting {h['host']}…")
    # quietly first: keys, the agent, or your own SSH_ASKPASS answer without a terminal (one try at a password)
    rc, err = quiet([*argv(h, batch=not os.environ.get("SSH_ASKPASS"), timeout=timeout),
                     "-o", "NumberOfPasswordPrompts=1", "-N", h["host"]])  # ControlPersist: backgrounds once in
    if rc and (unreachable(err) or term([*argv(h, timeout=timeout), "-N", h["host"]]) != 0):
        # else ssh asks in the terminal (new host key, 2FA, a password); a dead network won't take a typed one
        raise OSError(err if err.startswith(h["host"]) else f"{h['host']}: {err}")
    LOGINS.append(h)


# --- SFTP v3 (draft-ietf-secsh-filexfer-02), the version every server speaks ---
def join(d, n):
    return f"{d.rstrip('/')}/{n}"


def isdir(a):
    return stat.S_ISDIR(a.get("perm", 0))


def islink(a):
    return stat.S_ISLNK(a.get("perm", 0))


def special(a):  # pipe, socket or device: opening one could block forever, so transfers never do
    return stat.S_IFMT(a.get("perm", 0)) in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK)


def pack(*xs):  # ints -> uint32, str/bytes -> length-prefixed string; str round-trips raw bytes
    out = b""
    for x in xs:
        if isinstance(x, int):
            out += struct.pack(">I", x)
        else:
            x = x if isinstance(x, bytes) else x.encode(errors="surrogateescape")
            out += struct.pack(">I", len(x)) + x
    return out


class Reader:
    def __init__(r, data):
        r.d, r.i = data, 0

    def take(r, n):
        r.i += n
        return r.d[r.i - n:r.i]

    def u32(r):
        return struct.unpack(">I", r.take(4))[0]

    def str(r):
        return r.take(r.u32())

    def attrs(r):  # only size and type matter here
        f, a = r.u32(), {}
        if f & 1:
            a["size"] = struct.unpack(">Q", r.take(8))[0]
        if f & 2:
            r.take(8)  # uid, gid
        if f & 4:
            a["perm"] = r.u32()
        if f & 8:
            r.take(8)  # atime, mtime
        if f & 0x80000000:
            for _ in range(2 * r.u32()):
                r.str()  # extended name/value pairs
        return a


class SFTP:  # over `ssh -s host sftp` on the host's master: no remote shell, exact byte names, any SFTP server
    CHUNK, WINDOW = 32768, 64  # what OpenSSH's sftp uses: 2 MB in flight, and every server accepts 32 KB

    def __init__(s, h, timeout=8):
        s.err = tempfile.TemporaryFile()  # a file, not a pipe: ssh can never block on a full stderr
        s.p = subprocess.Popen([*argv(h, batch=True, timeout=timeout), "-s", h["host"], "sftp"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=s.err,
                               start_new_session=True)  # ^C is for rshm: stop the transfer, don't kill the link
        s.n, s.early, s.ext = 0, {}, set()

    def init(s):
        s.send(1, pack(3))  # INIT v3 -> VERSION + the extensions the server offers
        r = s.recv()[1]
        r.u32()
        while r.i < len(r.d):
            s.ext.add(r.str())
            r.str()
        return s

    def close(s):
        s.p.kill()
        s.p.wait()

    def fail(s, why=None):
        s.close()
        raise OSError(why or last(s.err) or "connection closed")

    def send(s, t, data):
        try:
            s.p.stdin.write(struct.pack(">IB", len(data) + 1, t) + data)
            s.p.stdin.flush()
        except OSError:
            s.fail()

    def read(s, n):
        b = s.p.stdout.read(n)
        if len(b) < n:
            s.fail()
        return b

    def recv(s):
        head = s.read(4)
        n = struct.unpack(">I", head)[0]
        if n > 1 << 24:  # not a packet: the login shell printed text first (sftp-server starts through it)
            s.fail(f"server printed {head + s.p.stdout.read1(40)!r}… before SFTP: a startup file like .bashrc echoes")
        body = s.read(n)
        return body[0], Reader(body[1:])

    def req(s, t, *args):  # send a request, don't wait -> its id
        s.n += 1
        try:
            s.send(t, pack(s.n, *args))
        except BaseException:
            s.close()
            raise
        return s.n

    def wait(s, i):  # the reply to request i -> Reader, None for OK/EOF, OSError for anything else
        try:
            while i not in s.early:
                rt, r = s.recv()
                s.early[r.u32()] = rt, r
        except BaseException:  # Ctrl-C or a dead link mid-packet: the stream is out of sync, drop it
            s.close()
            raise
        rt, r = s.early.pop(i)
        if rt != 101:  # anything but STATUS
            return r
        code = r.u32()
        if code > 1:
            raise OSError(r.str().decode(errors="replace") or f"SFTP status {code}")

    def call(s, t, *args):
        return s.wait(s.req(t, *args))

    def release(s, h):  # CLOSE a handle, unless the session already died (^C, lost link): that error must win
        if s.p.poll() is None:
            s.call(4, h)

    def realpath(s, p):
        r = s.call(16, p)
        r.u32()
        return r.str().decode(errors="surrogateescape")

    def stat(s, p, follow=True):
        return s.call(17 if follow else 7, p).attrs()

    def listdir(s, d):  # -> [(name, attrs)]; attrs describe links themselves, not their targets
        h, out = s.call(11, d).str(), []
        try:
            while r := s.call(12, h):
                for _ in range(r.u32()):
                    n, _, a = r.str(), r.str(), r.attrs()
                    if n not in (b"", b".", b"..") and b"/" not in n:  # real entries only
                        out.append((n.decode(errors="surrogateescape"), a))
        finally:
            s.release(h)
        return out

    def mkdir(s, p):
        s.call(14, p, 0)

    def rename(s, a, b):  # replace b: atomically where the server can (OpenSSH), else remove then rename
        if b"posix-rename@openssh.com" in s.ext:
            return s.call(200, "posix-rename@openssh.com", a, b)
        try:
            s.call(13, b)
        except OSError:
            pass
        s.call(18, a, b)

    def peek(s, p):  # attrs of whatever is at p (links not followed), None if nothing is
        try:
            return s.stat(p, follow=False)
        except OSError:
            return None

    def get(s, rp, lp, tick, size=None, mode=0o666):  # rp -> lp, appending: what lp already holds is resumed, not refetched
        size = s.stat(rp).get("size", 0) if size is None else size
        h = s.call(3, rp, 1, 0).str()  # OPEN for READ
        try:
            with open(lp, "ab", opener=lambda p, fl: os.open(p, fl, mode | 0o200)) as f:
                if f.tell() > size:
                    f.truncate(0)  # bigger than the source: not a partial of it, start over
                    f.seek(0)
                q, off = deque(), f.tell()
                tick(rp, off, resumed=True)
                while off < size or q:
                    while off < size and len(q) < s.WINDOW:
                        n = min(s.CHUNK, size - off)
                        q.append((s.req(5, h, off >> 32, off & 0xFFFFFFFF, n), off, n))
                        off += n
                    i, o, n = q.popleft()
                    data = (r := s.wait(i)) and r.str() or b""
                    while len(data) < n:  # short read: fetch the rest before writing on
                        more = (r := s.call(5, h, (o + len(data)) >> 32, (o + len(data)) & 0xFFFFFFFF,
                                               n - len(data))) and r.str()
                        if not more:
                            raise OSError(f"{rp}: file shrank while being read")
                        data += more
                    f.write(data)
                    tick(rp, o + len(data))
        finally:
            s.release(h)

    def put(s, lp, rp, tick, off, mode):  # lp -> rp, continuing a partial rp that is `off` bytes long
        with open(lp, "rb") as f:
            if off > os.fstat(f.fileno()).st_size:
                off = 0  # not a prefix of this file: start over
            tick(lp, off, resumed=True)
            h = s.call(3, rp, 2 | 8 | (0x10 if off == 0 else 0), 0).str()  # WRITE | CREAT, TRUNC unless resuming
            try:
                f.seek(off)
                q = deque()
                for chunk in iter(lambda: f.read(s.CHUNK), b""):
                    off += len(chunk)
                    q.append((s.req(6, h, (off - len(chunk)) >> 32, (off - len(chunk)) & 0xFFFFFFFF, chunk), off))
                    if len(q) == s.WINDOW:
                        i, end = q.popleft()
                        s.wait(i)
                        tick(lp, end)
                perm = s.req(10, h, 4, mode)  # FSETSTAT permissions: rides along with the last writes, no extra wait
                while q:
                    i, end = q.popleft()
                    s.wait(i)
                    tick(lp, end)
                try:
                    s.wait(perm)
                except OSError:
                    pass  # a server that won't set permissions (some Windows builds) still gets the file
            finally:
                s.release(h)

    def rm(s, p, a=None):  # recursive; never follows a symlink out of the tree
        if isdir(a or s.stat(p, follow=False)):
            for n, a in s.listdir(p):
                s.rm(join(p, n), a)
            s.call(15, p)
        else:
            s.call(13, p)


def fetch(s, rp, lp, tick, a=None):  # remote file or tree -> local; each file lands as NAME.rshm-part, renamed once complete
    a = a or s.stat(rp)  # a tree walk already has every entry's attrs: no round trip to ask again
    if special(a):
        raise OSError(f"{rp}: not a regular file")
    if not isdir(a):
        if rp not in tick.finished:  # a reconnect walks the tree again: skip what already made it
            s.get(rp, lp + PART, tick, a.get("size"), a.get("perm", 0o666) & 0o777)
            os.replace(lp + PART, lp)
            tick.finished.add(rp)
        return
    os.makedirs(lp, exist_ok=True)
    for n, a in s.listdir(rp):
        if islink(a):
            try:
                a = s.stat(join(rp, n))
            except OSError:
                continue  # dangling
            if isdir(a):
                continue  # ponytail: linked folders inside a tree are skipped, so a link loop can't recurse
        if not special(a):
            fetch(s, join(rp, n), os.path.join(lp, n), tick, a)


def send(s, lp, rp, tick, there=None):  # local file or tree -> remote; `there`: rp's folder listing, if already known
    if not os.path.isdir(lp) and not os.path.isfile(lp):
        raise OSError(f"{lp}: not a regular file")
    if not os.path.isdir(lp):
        if lp in tick.finished:  # a reconnect walks the tree again: skip what already made it
            return
        name = rp.rsplit("/", 1)[-1]
        old = there.get(name) if there is not None else s.peek(rp)
        if old and islink(old):  # write through a link, like cp: the link stays, its target gets the new content
            rp, there = s.realpath(rp), None
            old = s.stat(rp)
        part = there.get(name + PART) if there is not None else s.peek(rp + PART)
        mode = (old or {}).get("perm") or os.stat(lp).st_mode  # replacing a file keeps its permissions: 600 stays 600
        s.put(lp, rp + PART, tick, (part or {}).get("size", 0), mode & 0o7777)
        s.rename(rp + PART, rp)  # ponytail: the replaced file is now owned by whoever uploaded it
        return tick.finished.add(lp)
    try:
        s.mkdir(rp)
        there = {}  # brand new folder: nothing in it to look up
    except OSError:
        if not isdir(s.stat(rp)):
            raise
        there = dict(s.listdir(rp))  # one listing instead of two lookups per file
    for n in sorted(os.listdir(lp)):
        p = os.path.join(lp, n)
        if (os.path.isfile(p) or os.path.isdir(p)) and not (os.path.islink(p) and os.path.isdir(p)):
            send(s, p, join(rp, n), tick, there)  # regular files and real folders only: no pipes, loops or dangling links


# --- TUI ---
def codes(keys):  # "x Del" -> every keycode it stands for
    return [c for k in keys.split() for c in (NAMES[k] if k in NAMES else (ord(k),))]


def editor(var):  # $EDITOR or $PAGER as the user set it (flags allowed), else the classic
    return shlex.split(os.environ.get(var) or "") or [{"EDITOR": "vi", "PAGER": "less"}[var]]


def lcwd():  # the local folder; if someone removed it underneath us, home
    try:
        return os.getcwd()
    except OSError:
        os.chdir(Path.home())
        return os.getcwd()


def safe(name):  # remote names are arbitrary bytes: never let one drive the terminal
    return "".join(c if c.isprintable() else "?" for c in name)


def human(n):  # 1536 -> 1.5K
    if n is None:
        return ""
    for u in "BKMGT":
        if n < 1024 or u == "T":
            return f"{n}B" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


class App:
    def __init__(s, scr, cfg):
        s.cfg, s.scr, s.closed, s.msg, s.flt, s.typing = cfg, scr, set(), "", ["", ""], False  # flt: hosts, files
        s.sel = s.top = s.fsel = s.ftop = s.focus = s.L = 0
        s.host = s.hpath = s.sftp = None
        s.cwd, s.files, s.size, s.hosts, s.theme_name = "", [], {}, {}, "default"
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
        s.panes = [[(k, what, getattr(s, m) if m else None) for k, what, m in KEYS[p] + KEYS["both"]] for p in ("hosts", "files")]
        s.keymap = [{c: fn for keys, _, fn in pane if fn for c in codes(keys)} for pane in s.panes]
        s.log, s.working, s.frame, s.drawing, s.n = deque(maxlen=3), None, 0, False, 1
        signal.signal(signal.SIGALRM, s.spin)
        s.load()
        s.closed = groups(s.hosts)  # folded: only hosts outside any group show
        s.sync()

    def load(s):
        try:
            c = read(s.cfg)
            s.hosts, s.theme_name = hosts(c), str(c.get("theme", s.theme_name))
        except (OSError, tomllib.TOMLDecodeError) as e:
            s.msg = f"config error: {e}"
        s.set_theme(s.theme_name)
        s.sync()

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
        save(s.cfg, retheme(s.cfg.read_text(), s.theme_name))
        s.msg = f"theme: {s.theme_name}" + (" (mono: terminal lacks colors)" if s.C is MONO else "")

    def sync(s):  # each pane its own filter; ponytail: a hosts filter unfolds every group (set()), s.closed untouched
        hq, fq = s.flt[0].lower(), s.flt[1].lower()
        s.rows = [r for r in flat(s.hosts, set() if hq else s.closed) if match(r, hq)]
        s.shown = [f for f in s.files if f == "../" or fq in f.lower()]

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

    def tty(s, fn, *a, **k):  # hand the terminal over (prompts, pinentry, editors), take it back
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

    def term(s, cmd):
        return s.tty(subprocess.call, cmd)

    def connect(s, h, timeout=8):
        connect(h, s.term, timeout, s.busy)

    def shell(s, h, d=""):
        s.connect(h)
        s.term(sshcmd(h, d))

    def fs(s):  # the SFTP session of the browsed host, (re)opened on demand
        if not (s.sftp and s.sftp.p.poll() is None):
            s.connect(s.host)
            s.sftp = SFTP(s.host).init()
        return s.sftp

    def ticker(s, label, total):  # progress in the status bar; transfers report how far each file is
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

    def xfer(s, fn, src, dst, label, total=None):  # a dropped link reconnects and resumes where it stopped
        tick, fails, seen, t0 = s.ticker(label, total), 0, 0, time.monotonic()
        s.busy(label)
        while True:
            try:
                f = s.fs()
            except OSError as e:
                if not unreachable(str(e)):
                    raise  # wrong password, host key...: retrying won't help
                err = e
            else:
                try:
                    fn(f, src, dst, tick)
                    break
                except OSError as e:
                    if f.p.poll() is None:
                        raise  # the session is fine: a real refusal (permissions, disk full), not the link
                    err = e
            fails, seen = (1 if tick.done > seen else fails + 1), tick.done  # progress since the last drop resets it
            if fails > 5:
                raise err
            s.msg = f"✗ {label}: link lost ({err})"
            s.busy(f"{label}: reconnecting in {min(2 ** fails, 20)}s, then resuming")
            time.sleep(min(2 ** fails, 20))
        n = f", {len(tick.finished)} files" if len(tick.finished) > 1 else ""
        s.msg = f"✓ {label}  {human(tick.done)}{n} in {time.monotonic() - t0:.0f}s"

    # --- hosts pane ---
    def row(s):
        return s.rows[s.sel] if s.rows else (0, "", None, ())

    def enter(s):  # host: ssh; group: fold / unfold
        _, _, h, p = s.row()
        if h:
            s.shell(h)
        elif p:
            s.closed ^= {p}
            s.sync()

    def hopen(s):  # host: browse its files; group: unfold
        _, _, h, p = s.row()
        if h:
            if s.sftp:
                s.sftp.close()
            s.host, s.hpath, s.sftp, s.files, s.cwd, s.focus = h, p, None, [], "", 0
            s.sync()
            s.ls(h.get("_cwd") or h.get("path", ""))  # back where you left this host
            s.focus = 1
        else:
            s.closed.discard(p)
            s.sync()

    def hclose(s):  # open group: fold; anything else: jump to its group
        _, _, h, p = s.row()
        if not h and p and p not in s.closed:
            s.closed.add(p)
            s.sync()
        elif len(p) > 1:
            s.sel = next((i for i, r in enumerate(s.rows) if r[3] == p[:-1]), s.sel)

    def ping(s):  # live check: one round trip to the host itself, by the same route as a session (jumps included)
        h = s.row()[2]
        if h:
            s.connect(h, timeout=3)
            s.busy(f"ping {h['host']}")
            t0 = time.monotonic()
            rc, err = quiet([*argv(h, batch=True, timeout=3), h["host"], "true"])
            if rc:
                raise OSError(f"{h['host']}: {err}")
            s.msg = f"✓ {h['host']} answers ({(time.monotonic() - t0) * 1000:.0f} ms)"

    def copyid(s):  # install your key; through the master, so jumps and passwords just work
        h = s.row()[2]
        if h:
            s.connect(h)
            # -f: through the master every key "already works", which would fool ssh-copy-id's check
            rc = s.term(["ssh-copy-id", "-f", *argv(h)[1:], h["host"]])
            s.msg = "✓ key installed" if rc == 0 else f"✗ ssh-copy-id failed ({rc})"

    def edit(s):
        s.term([*editor("EDITOR"), str(s.cfg)])
        s.load()

    # --- files pane ---
    def cur(s):
        return s.shown[s.fsel] if s.shown else "../"

    def ls(s, d):
        f = s.fs()
        s.busy(f"listing {safe(d) or '~'}")
        d = f.realpath(re.sub(r"^~(/|$)", "", d) or ".")  # sftp starts in $HOME, so ~/x is just x
        files, s.size = [], {}
        for n, a in f.listdir(d):
            if islink(a):  # a link to a folder browses like one (delete still removes only the link)
                try:
                    a = f.stat(join(d, n))
                except OSError:
                    pass  # dangling: stays a plain entry
            if isdir(a):
                files.append(n + "/")
            else:
                files.append(n)
                s.size[n] = a.get("size")
        files.sort(key=lambda n: (not n.endswith("/"), n.casefold()))
        if d != s.cwd:
            s.flt[1] = ""  # another folder: the files filter was for the old one's names
        s.cwd, s.files, s.fsel, s.ftop = d, ["../", *files], 0, 0
        s.host["_cwd"] = d
        s.sync()

    def fopen(s):
        f = s.cur()
        if f == "../":
            s.parent()
        elif f.endswith("/"):
            s.ls(join(s.cwd, f[:-1]))
        else:
            s.local(False)

    def parent(s):
        s.ls(s.cwd.rsplit("/", 1)[0] or "/")

    def local(s, edit):  # a private temp copy in *your* $PAGER/$EDITOR, saved back atomically if it changed
        f = s.cur()
        size = s.size.get(f) or 0
        if f.endswith("/") or size > BIG and s.ask(f"{safe(f)} is {human(size)}: fetch it all? (y/N): ").lower() != "y":
            return
        d = tempfile.mkdtemp()  # 0700, private; removed afterwards unless it holds an edit that couldn't be saved
        p, keep = os.path.join(d, f), False
        try:
            s.xfer(fetch, join(s.cwd, f), p, f"↓ {safe(f)}", size)
            with open(p, "rb") as fh:
                binary = b"\0" in fh.read(8192)
            if edit and binary and s.ask("binary file: a text editor may mangle it. edit anyway? (y/N): ").lower() != "y":
                return
            t0 = os.stat(p).st_mtime_ns
            s.term([*editor("PAGER" if not edit else "EDITOR"), p])
            if edit and os.stat(p).st_mtime_ns != t0:
                try:
                    s.xfer(send, p, join(s.cwd, f), f"↑ {safe(f)}", os.path.getsize(p))
                except BaseException as e:
                    keep = True
                    raise OSError(f"not saved ({e or 'interrupted'}): your edit is in {safe(p)}") from None
                s.msg = f"✓ saved {safe(f)}"
        finally:
            if not keep:
                shutil.rmtree(d, ignore_errors=True)

    def view(s):
        s.local(False)

    def fedit(s):
        s.local(True)

    def here(s):
        s.shell(s.host, s.cwd)

    def home(s):
        s.ls("")

    def get(s):
        f = s.cur().rstrip("/")
        lp = os.path.join(lcwd(), f)
        if f != ".." and (not os.path.lexists(lp) or s.ask(f"overwrite local {safe(f)}? (y/N): ").lower() == "y"):
            s.xfer(fetch, join(s.cwd, f), lp, f"↓ {safe(f)}", s.size.get(f))

    def put(s):
        src = s.ask(f"upload (from {lcwd()}): ")
        if src:
            src = os.path.abspath(os.path.expanduser(src))
            if not os.path.exists(src):
                raise OSError(f"{src}: no such file")
            name = os.path.basename(src)
            if name in {f.rstrip("/") for f in s.files} and s.ask(f"overwrite {safe(name)}? (y/N): ").lower() != "y":
                return
            s.xfer(send, src, join(s.cwd, name), f"↑ {safe(name)}", None if os.path.isdir(src) else os.path.getsize(src))
            s.ls(s.cwd)

    def frename(s):  # a plain SFTP rename: refuses to overwrite; "sub/", "sub/x" or "/abs/x" move it
        f = s.cur().rstrip("/")
        new = f != ".." and s.ask(f"rename {safe(f)} to: ")
        if new:
            dst = new if new.startswith("/") else join(s.cwd, new)
            dst = join(dst, f) if new.endswith("/") else dst  # "sub/" moves it into sub, like mv
            if s.fs().peek(dst):
                raise OSError(f"{safe(new)} already exists")
            s.fs().call(18, join(s.cwd, f), dst)
            s.ls(s.cwd)
            s.msg = f"✓ renamed {safe(f)} to {safe(new)}"

    def fdel(s):
        f = s.cur()
        if f != "../" and s.ask(f"delete {safe(f)}? (y/N): ").lower() == "y":
            try:
                s.busy(f"deleting {safe(f)}")
                s.fs().rm(join(s.cwd, f.rstrip("/")))
                s.msg = f"✓ deleted {safe(f)}"
            finally:
                s.ls(s.cwd)  # show what's left even if it stopped halfway

    def fmkdir(s):
        name = s.ask("new remote dir: ").strip("/")
        if name:
            s.fs().mkdir(join(s.cwd, name))
            s.ls(s.cwd)
            s.msg = f"✓ created {safe(name)}"

    def goto(s):
        d = s.ask("remote path: ")
        if d:
            s.ls(d)

    def lcd(s):
        d = s.ask(f"local dir (now {lcwd()}): ")
        if d:
            os.chdir(os.path.expanduser(d))

    # --- both panes ---
    def reload(s):
        s.load()
        if s.host:
            s.ls(s.cwd)

    def tab(s):
        s.focus = int(not s.focus and bool(s.shown))

    def esc(s):  # clear this pane's filter; with none, back to the hosts
        if s.flt[s.focus]:
            s.flt[s.focus] = ""
            s.sync()
        else:
            s.focus = 0

    def quit(s):
        sys.exit()

    def move(s, d):
        if s.focus:
            s.fsel = max(0, min(len(s.shown) - 1, s.fsel + d))
        else:
            s.sel = max(0, min(len(s.rows) - 1, s.sel + d))

    def search(s):  # filter this pane: type to narrow, arrows move; ⏎ applies (never opens), Esc clears
        f = s.focus
        s.typing, s.flt[f] = True, ""
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
                    s.flt[f] = ""
                elif isinstance(c, int) and c in MOVES:
                    s.move(MOVES[c])
                    continue
                elif c in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    s.flt[f] = s.flt[f][:-1]
                elif isinstance(c, str) and c.isprintable():
                    s.flt[f] += c
                else:
                    continue
                s.sync()
                if f:
                    s.fsel = int(len(s.shown) > 1)  # first match, not the ever-present ../
                else:
                    s.sel = 0
                if c == "\x1b":
                    break
        finally:
            s.typing = False
            curses.curs_set(0)

    def ask(s, q):
        H, W = s.scr.getmaxyx()
        s.scr.move(H - 1, 0)
        s.scr.clrtoeol()
        s.scr.addnstr(H - 1, 0, q, W - 1, s.C[2] | curses.A_BOLD)
        curses.echo()
        curses.curs_set(1)
        try:
            with s.still():
                v = s.scr.getstr(H - 1, min(len(q), W - 2), 512).decode(errors="ignore").strip()
        except KeyboardInterrupt:
            v = ""
        curses.noecho()
        curses.curs_set(0)
        return v

    def ctx(s, y=None, x=None):  # menu of every action + its keys: right-click, `m` or `?`
        it = s.panes[s.focus]  # doc-only rows (move, mouse) show too: this menu is the help page
        H, W = s.scr.getmaxyx()
        if y is None:
            y, x = (s.fsel - s.ftop + 1, s.L + 3) if s.focus else (s.sel - s.top + 1, 2)
        w, h = min(max(len(l) + len(k) for k, l, _ in it) + 6, W), min(len(it) + 2, H)
        y, x = max(0, min(y, H - h)), max(0, min(x, W - w))
        win = curses.newwin(h, w, y, x)
        win.keypad(True)
        win.bkgd(" ", s.C[0])
        i = 0
        while True:
            off = max(0, i - h + 3)  # scroll when the terminal is shorter than the menu
            win.erase()
            win.box()
            for r, (k, l, _) in enumerate(it[off:off + h - 2]):
                win.addnstr(r + 1, 1, f" {l}".ljust(w - 3 - len(k)) + k + " ", w - 2, s.C[1] if r + off == i else s.C[0])
            c = win.getch()
            if c in MOVES:
                i = (i + MOVES[c]) % len(it)
            elif c in (10, 13, 32, curses.KEY_ENTER, curses.KEY_RIGHT):
                return it[i][2] and it[i][2]()
            elif c == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, b = curses.getmouse()
                except curses.error:
                    continue
                if b & (LEFT_CLICK | RIGHT_CLICK):
                    r = my - y - 1
                    return it[r + off][2] and it[r + off][2]() if x <= mx < x + w and 0 <= r < h - 2 else None
            else:
                return

    def key(s, c):
        if c == curses.KEY_MOUSE:
            s.mouse()
        elif c in MOVES:
            s.move(MOVES[c])
        elif fn := s.keymap[s.focus].get(c):
            fn()

    def mouse(s):
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return
        pane = int(x > s.L)
        if b & (WHEEL_UP | WHEEL_DOWN):
            s.focus = int(pane and bool(s.shown))
            return s.move(-3 if b & WHEEL_UP else 3)
        if not b & (LEFT_CLICK | RIGHT_CLICK) or not 0 < y <= s.n:
            return
        j = (s.ftop if pane else s.top) + y - 1
        if j < len(s.shown if pane else s.rows):
            again = s.focus == pane and j == (s.fsel if pane else s.sel)
            s.focus = pane
            if pane:
                s.fsel = j
            else:
                s.sel = j
            if b & LEFT_CLICK:  # file: open on second click; host: browse, then ssh; group: fold
                if pane:
                    if again:
                        s.fopen()
                elif s.rows[j][2] and s.rows[j][3] != s.hpath:
                    s.hopen()
                else:
                    s.enter()
                return
        if b & RIGHT_CLICK:
            s.ctx(y, x)

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
        L = s.L = min(36, max(24, W // 3))
        logn = 3 if H >= 18 else 1  # activity panel lines
        n, fw = H - 3 - logn, W - L - 2  # rows between the borders, file column width
        s.n = n
        s.sel = min(s.sel, max(len(s.rows) - 1, 0))
        s.fsel = min(s.fsel, max(len(s.shown) - 1, 0))
        s.top = min(max(s.top, s.sel - n + 1), s.sel)
        s.ftop = min(max(s.ftop, s.fsel - n + 1), s.fsel)
        htag, ftag = (f" /{q}" if q else "" for q in s.flt)
        hint = "  ".join(f"{k.split()[0]} {l}" for k, l, _ in s.panes[s.focus][:6]) + "  ? more"
        try:
            sc.addnstr(0, 0, f"┌─ rshm ─ hosts {len(s.rows)}{htag} ".ljust(L, "─"), L, s.C[2 if not s.focus else 3])
            sc.addnstr(0, 3, "rshm", 4, s.C[5] | curses.A_BOLD)
            where = f"{s.host['host']}:{safe(s.cwd)}{ftag} " if s.host else ""
            sc.addnstr(0, L, f"┬─ {where}".ljust(W - L - 1, "─") + "┐", W - L, s.C[2 if s.focus else 3])
            for i in range(n):
                y, j, fj = i + 1, i + s.top, i + s.ftop
                if j < len(s.rows):
                    dep, k, h, p = s.rows[j]
                    ico = ("●" if p == s.hpath else "○") if h else ("▸" if p in s.closed else "▾")
                    at = s.C[1] if j == s.sel and not s.focus else curses.A_BOLD if j == s.sel else s.C[0 if h else 2]
                    sc.addnstr(y, 0, f" {'  ' * dep}{ico} {k}".ljust(L), L, at)
                sc.addstr(y, L, "│", s.C[3])
                if fj < len(s.shown):
                    f = s.shown[fj]
                    size = human(s.size.get(f))
                    at = s.C[1] if fj == s.fsel and s.focus else s.C[2 if f.endswith("/") else 0]
                    sc.addnstr(y, L + 1, f" {safe(f)}"[:fw - len(size) - 1].ljust(fw - len(size)) + size, fw, at)
            for x, top, total in ((L - 2, s.top, len(s.rows)), (W - 1, s.ftop, len(s.shown))):
                if top:
                    sc.addstr(1, x, "▲", s.C[2])
                if top + n < total:
                    sc.addstr(n, x, "▼", s.C[2])
            sc.addnstr(n + 1, 0, "└─ activity ".ljust(L, "─") + "┴".ljust(W - L - 1, "─") + "┘", W, s.C[3])
            keep = logn - bool(s.working)  # the live line, when there is one, takes the last row
            log = list(s.log)[-keep:] if keep else []
            for i, line in enumerate(log):
                sc.addnstr(H - 1 - len(log) - bool(s.working) + i, 0, f" {safe(line)}", W - 1, s.C[4 if "✗" in line else 3])
            s.paint_work()
            if s.typing:
                q = s.flt[s.focus]
                sc.addnstr(H - 1, 0, f" / {q}".ljust(W - 22) + "⏎ apply  Esc clear", W - 1, s.C[2] | curses.A_BOLD)
                sc.move(H - 1, min(3 + len(q), W - 2))
            else:
                local = f" local {lcwd()} │" if s.focus else ""
                sc.addnstr(H - 1, 0, f"{local} {hint}".ljust(W - 1), W - 1, s.C[3])
        except curses.error:
            pass  # a cell off-screen mid-resize; next frame fixes it

    def loop(s):
        while True:
            s.draw()
            c, t0 = s.scr.getch(), time.monotonic()  # Ctrl-C here (idle) quits
            try:
                s.key(c)
            except KeyboardInterrupt:  # Ctrl-C mid-action cancels just the action
                s.msg = "✗ interrupted"
            except Exception as e:  # event-loop boundary: report it, keep the session
                s.msg = f"✗ {e}" if isinstance(e, OSError) else f"✗ {type(e).__name__}: {e}"
            finally:
                s.idle()
                if time.monotonic() - t0 > 0.5:
                    curses.flushinp()  # keys hit while a long action ran shouldn't fire now


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="rshm", description="Remote SHell Manager: SSH hosts in one TOML file, one screen.",
                                 epilog=keys_help(), formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="?", help="ssh straight into HOST (name, group/name or unique substring)")
    ap.add_argument("-c", "--config", type=Path, default=CFG, help=f"config file (default: {CFG})")
    a = ap.parse_args()
    cfg = a.config.expanduser()
    if cfg != CFG and not cfg.exists():  # only the default config is created: a typo makes no stray files
        ap.error(f"{cfg}: no such file")
    (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)  # the ControlPath sockets live here
    for sig in (signal.SIGTERM, signal.SIGHUP):  # kill or a closed terminal unwind normally: terminal and logins cleaned
        signal.signal(sig, lambda n, _: sys.exit(128 + n))
    atexit.register(logout)
    if a.host:
        try:
            hs = hosts(read(cfg))
        except (OSError, tomllib.TOMLDecodeError) as e:
            ap.error(f"{cfg}: {e}")
        m = find(hs, a.host)
        if len(m) != 1:
            ap.error(f"ambiguous host {a.host!r}: {', '.join(p for p, _ in m)}" if m else f"no host {a.host!r} in {cfg}")
        try:
            connect(m[0][1], subprocess.call)
            sys.exit(subprocess.call(sshcmd(m[0][1])))
        except OSError as e:
            sys.exit(f"rshm: {e}")
        except KeyboardInterrupt:
            sys.exit(130)
    try:
        curses.wrapper(lambda scr: App(scr, cfg).loop())
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        sys.exit(f"rshm needs a terminal ({e}); `rshm HOST` works without one")


if __name__ == "__main__":
    main()
