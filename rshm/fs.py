"""One file system interface, three of them: Local (this machine), Remote (a host, over SFTP) and Archive (a zip or
tar, read-only). Everything a pane does goes through these primitives, so copy, move and delete work between any two.

    name                                its label
    realpath(p) ("" = home)  stat(p, follow)  listdir(d)  readlink(p)  free(p) -> bytes or None
    mkdir(p)  rmdir(p)  unlink(p)  rename(a, b, replace)  symlink(target, p)  hardlink(src, p)
    chattr(p, ch)                       ch: {"perm", "uid", "gid", "mtime"}, the one door for attributes
    read(p, off, size) -> chunks        write(p, chunks, off, ch, done)
    run(script, d) -> (argv, cwd)       a shell command in folder d, on the terminal
    alive()  close()                    alive: False once a session dropped (a transfer reconnects and resumes);
                                        a lost link raises ConnectionError, never an answer like "no such file"
attrs: {"size", "perm", "mtime", "uid", "gid", "user", "group", "nlink"}, each only when known; look() adds a link's
"target" and "dest" (what it points to; None when nothing).
"""
import contextlib, functools, grp, itertools, lzma, os, posixpath, pwd, re, shlex, stat, tarfile, time, zipfile, zlib
from collections import defaultdict

from .sftp import CHUNK, SFTP
from .ssh import argv, connect

PART = ".rshm-part"  # a file in transit: resumed from, renamed into place once complete
ARCHIVES = (".zip", ".jar", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")
BROKEN = (tarfile.TarError, zipfile.BadZipFile, EOFError, KeyError, lzma.LZMAError, zlib.error)  # an archive's damage
MEDIA = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp3", ".flac", ".ogg", ".wav", ".mp4", ".mkv", ".webm",
         ".avi", ".mov")


def isdir(a):
    return stat.S_ISDIR(a.get("perm", 0))


def islink(a):
    return stat.S_ISLNK(a.get("perm", 0))


def special(a):  # pipe, socket or device: opening one could block forever, so transfers never do
    return stat.S_IFMT(a.get("perm", 0)) in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK)


def folder(a):  # browses like a folder: one, or a link to one (.. has no attrs)
    return not a or isdir(a.get("dest") or a)


def kind(name, a):  # what an entry is, for its colour: the one place that decides
    if not a or isdir(a):
        return "dir"
    if islink(a):
        return "link" if a.get("dest") else "stale"
    n = name.lower()
    return ("special" if special(a) else "exe" if a.get("perm", 0) & 0o111 else "archive" if n.endswith(ARCHIVES)
            else "media" if n.endswith(MEDIA) else "hidden" if n[:1] == "." else "file")


def marker(a):  # ls -F: / folder, @ link, * executable, | fifo, = socket
    t = stat.S_IFMT(a.get("perm", 0)) if a else stat.S_IFDIR
    return {stat.S_IFDIR: "/", stat.S_IFLNK: "@", stat.S_IFIFO: "|", stat.S_IFSOCK: "="}.get(
        t, "*" if t == stat.S_IFREG and a["perm"] & 0o111 else "")


def join(d, n):
    return f"{d.rstrip('/')}/{n}"


@functools.cache
def names(uid, gid):  # this machine's user and group names for two ids; the number when there is none
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    try:
        return user, grp.getgrgid(gid).gr_name
    except KeyError:
        return user, str(gid)


def attrs(st):
    user, group = names(st.st_uid, st.st_gid)
    return {"size": st.st_size, "perm": st.st_mode, "mtime": int(st.st_mtime), "uid": st.st_uid, "gid": st.st_gid,
            "user": user, "group": group, "nlink": st.st_nlink}


class Local:
    name = "local"

    def realpath(s, p):
        return os.path.realpath(os.path.expanduser(p or "~"))

    def stat(s, p, follow=True):
        return attrs(os.stat(p) if follow else os.lstat(p))

    def listdir(s, d):
        with os.scandir(d) as it:
            return [(e.name, attrs(e.stat(follow_symlinks=False))) for e in it]

    def readlink(s, p):
        return os.readlink(p)

    def free(s, p):
        v = os.statvfs(p)
        return v.f_frsize * v.f_bavail

    def mkdir(s, p):
        os.mkdir(p)

    def rmdir(s, p):
        os.rmdir(p)

    def unlink(s, p):
        os.unlink(p)

    def rename(s, a, b, replace=True):
        if not replace and os.path.lexists(b):
            raise FileExistsError(f"{b}: already exists")
        os.rename(a, b)

    def symlink(s, target, p):
        os.symlink(target, p)

    def hardlink(s, src, p):
        os.link(src, p, follow_symlinks=False)

    def chattr(s, p, ch):
        if "uid" in ch or "gid" in ch:
            os.chown(p, ch.get("uid", -1), ch.get("gid", -1))
        if "perm" in ch:  # after the owner: chown may drop setuid bits
            os.chmod(p, ch["perm"])
        if "mtime" in ch:
            os.utime(p, (ch["mtime"], ch["mtime"]))

    def read(s, p, off, size):
        with open(p, "rb") as f:
            f.seek(off)
            while off < size:
                b = f.read(min(CHUNK, size - off))
                if not b:
                    raise OSError(f"{p}: file shrank while being read")
                off += len(b)
                yield b

    def write(s, p, chunks, off, ch, done):  # private while it grows (a resume appends to it), then exactly ch
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | (0 if off else os.O_TRUNC), 0o600)
        with open(fd, "wb") as f:
            f.seek(off)
            for b in chunks:
                f.write(b)
                off += len(b)
                done(off)
        s.chattr(p, ch)

    def run(s, script, d):
        return ["sh", "-c", script], d

    def alive(s):
        return True

    def close(s):
        pass


class Remote:  # a host; its SFTP session opens on first use and reopens after a drop
    PRIMS = {"stat", "listdir", "readlink", "free", "mkdir", "rmdir", "unlink", "rename", "symlink", "hardlink", "chattr",
             "read", "write"}

    def __init__(s, h, tty, say):  # tty(fn, *a): fn run with the terminal handed over; say(what): what's going on
        s.h, s.tty, s.say, s.sf, s.name = h, tty, say, None, h["host"]

    def sftp(s):
        if not (s.sf and s.sf.alive()):
            s.sf = None  # until the new session is up: a failed login is a refusal, not a dropped link
            s.login()
            s.sf = SFTP(s.h).init()
        return s.sf

    def login(s):  # the host's master, logged in
        connect(s.h, s.tty, s.say)

    def __getattr__(s, n):  # the primitives are the session's own
        if n not in Remote.PRIMS:
            raise AttributeError(n)
        return getattr(s.sftp(), n)

    def realpath(s, p):
        return s.sftp().realpath(re.sub(r"^~(/|$)", "", p) or ".")  # sftp starts in $HOME, so ~/x is just x

    def run(s, script, d):  # in sh whatever the login shell (fish, csh): `cd &&` and the script's own quoting hold
        s.login()
        script = f"cd {shlex.quote(d)} && {script}" if d else script
        return [*argv(s.h), "-t", s.h["host"], f"exec sh -c {shlex.quote(script)}"], None

    def alive(s):
        return s.sf is None or s.sf.alive()

    def close(s):
        if s.sf:
            s.sf.close()

    def twin(s):  # a second session to the same host, on the same login
        return Remote(s.h, s.tty, s.say)


class Archive:  # a zip or tar file (gz, bz2, xz), browsed read-only like a folder; Python reads them all itself
    def __init__(s, path, name):
        s.name, s.files = name, {"/": {"perm": stat.S_IFDIR | 0o755, "size": 0}}  # every entry by its path inside
        entries = []  # (name inside, attrs); a link's target as "link"
        try:
            if zipfile.is_zipfile(path):
                s.z = zipfile.ZipFile(path)
                for i in s.z.infolist():
                    a = {"size": i.file_size, "ref": i, "mtime": int(time.mktime((*i.date_time, 0, 0, -1))),
                         "perm": i.external_attr >> 16 or (stat.S_IFDIR if i.is_dir() else stat.S_IFREG) | 0o644}
                    if islink(a):  # zip -y: the target is the entry's content
                        a["link"] = s.z.read(i).decode(errors="surrogateescape")
                    entries.append((i.filename, a))
            else:
                s.z = tarfile.open(path)
                for m in s.z.getmembers():
                    kind = stat.S_IFDIR if m.isdir() else stat.S_IFLNK if m.issym() else stat.S_IFREG if m.isfile() \
                        or m.islnk() else stat.S_IFIFO
                    size = s.z.getmember(m.linkname).size if m.islnk() else m.size  # a hard link reads as its target
                    entries.append((m.name, {"size": size, "ref": m, "perm": kind | m.mode, "mtime": m.mtime, "uid": m.uid,
                                             "gid": m.gid, "user": m.uname, "group": m.gname}
                                    | ({"link": m.linkname} if m.issym() else {})))
                while s.z.fileobj.read(1 << 20):  # on to its end, where gzip checks what it held (a tar has no check)
                    pass
        except (*BROKEN, OSError) as e:
            raise OSError(f"{name}: unreadable archive ({str(e).splitlines()[0].rstrip(':')})") from None
        for n, a in entries:  # "./x" and "x" are the same entry; "./" is the top itself
            if (p := posixpath.normpath("/" + n)) != "/":
                s.files[p] = a
        for p in list(s.files):  # folders only implied by the paths in them
            while (p := posixpath.dirname(p)) not in s.files:
                s.files[p] = {"perm": stat.S_IFDIR | 0o755, "size": 0}
        s.kids = defaultdict(list)
        for p, a in s.files.items():
            if p != "/":
                s.kids[posixpath.dirname(p)].append((posixpath.basename(p), a))

    def realpath(s, p):
        return posixpath.normpath("/" + p.strip("/"))

    def stat(s, p, follow=True):
        p = s.realpath(p)
        for _ in range(8):  # a link inside points within the archive; a loop gives up
            a = s.files.get(p)
            if a is None or not (follow and islink(a)):
                break
            p = s.realpath(posixpath.join(posixpath.dirname(p), a["link"]))
        if a is None or follow and islink(a):
            raise FileNotFoundError(f"{p}: not in {s.name}")
        return a

    def listdir(s, d):
        return list(s.kids[s.realpath(d)])

    def readlink(s, p):
        a = s.stat(p, follow=False)
        if "link" not in a:
            raise OSError(f"{p}: not a symbolic link")
        return a["link"]

    def free(s, p):
        return None

    def read(s, p, off, size):
        ref = s.stat(p)["ref"]
        try:
            with (s.z.open(ref) if isinstance(s.z, zipfile.ZipFile) else s.z.extractfile(ref)) as f:
                f.seek(off)
                while off < size:
                    b = f.read(min(CHUNK, size - off))
                    if not b:
                        raise OSError(f"{p}: truncated in archive")
                    off += len(b)
                    yield b
        except BROKEN as e:
            raise OSError(f"{p}: corrupt in {s.name} ({e})") from None

    def readonly(s, *_, **__):
        raise OSError(f"{s.name}: read-only archive")

    mkdir = rmdir = unlink = rename = symlink = hardlink = chattr = write = run = readonly

    def alive(s):
        return True

    def close(s):
        s.z.close()


def peek(fs, p):  # attrs of whatever is at p (links not followed), None if nothing is; a lost link is no answer
    try:
        return fs.stat(p, follow=False)
    except ConnectionError:
        raise
    except OSError:
        return None


def look(fs, d):  # a folder's entries, each link with its target and what it points to (dest; None: nothing)
    out = []
    for n, a in fs.listdir(d):
        if islink(a):
            a = {**a, "target": fs.readlink(join(d, n))}
            try:
                a["dest"] = fs.stat(join(d, n))
            except ConnectionError:
                raise
            except OSError:
                a["dest"] = None
        out.append((n, a))
    return out


def complete(fs, base, t):  # what path t (typed from folder base) can go on to: names there that start like it,
    head, slash, stem = t.rpartition("/")  # a folder (or a link to one) ending in /, dotfiles only for a dot
    if not slash:
        d = base
    elif t[:1] in "/~":  # from the top, or from home
        d = head or "/"
    else:
        d = join(base, head)
    try:
        d = fs.realpath(d)
        out = []
        for n, a in fs.listdir(d):
            if n.startswith(stem) and (n[:1] != "." or stem[:1] == "."):
                with contextlib.suppress(OSError):
                    a = fs.stat(join(d, n)) if islink(a) else a  # a link to a folder completes as one
                out.append(t[:len(t) - len(stem)] + n + "/" * isdir(a))
        return sorted(out)
    except OSError:  # not a folder, not readable, the link gone: nothing to offer, the typing goes on
        return []


def walk(fs, d, rel=""):  # every entry under d, as (path relative to d, attrs, as look() gives them); links not followed,
    try:                     # folders below it may not read skipped (d itself unreadable is an error)
        entries = look(fs, join(d, rel) if rel else d)
    except OSError as e:
        if not rel or isinstance(e, ConnectionError):
            raise
        return
    for n, a in entries:
        yield join(rel, n) if rel else n, a
        if isdir(a):
            yield from walk(fs, d, join(rel, n) if rel else n)


def rm(fs, p, a=None):  # recursive; a link is removed, never followed
    if isdir(a or fs.stat(p, follow=False)):
        for n, a in fs.listdir(p):
            rm(fs, join(p, n), a)
        fs.rmdir(p)
    else:
        fs.unlink(p)


def save(fs, p, data):  # p's content replaced at once (never half-written), its mode kept; a link written through
    old = peek(fs, p)
    if old and islink(old):
        p = fs.realpath(p)
        old = fs.stat(p)
    fs.write(p + PART, (data[i:i + CHUNK] for i in range(0, len(data), CHUNK)), 0,
             {"perm": (old or {}).get("perm", 0o644) & 0o777}, lambda at: None)
    fs.rename(p + PART, p)


def copy(src, sp, dst, dp, tick, follow=False):
    """src:sp -> dst:dp, a file or a tree, like cp -pP: content, mode and times travel; a link travels as a link
    (follow: what it points to instead). The source is looked at now, never trusted from an older listing. Each file
    lands as NAME.rshm-part, resumed if one is there, renamed into place when complete; a file it replaces takes the
    source's mode. tick(path, at, resumed) reports progress; tick.finished holds what already made it, so a retry
    after a dropped link skips it; tick.skipped gets the pipes, sockets and devices of a tree, which are never opened.
    Within one host the server copies by itself where it can (OpenSSH 9.0+), else a second session reads: one
    session's reads in flight would block its writes."""
    one = src is dst and isinstance(src, Remote)
    here = one and b"copy-data" in src.sftp().ext
    reader = src.twin() if one and not here else src
    try:
        _copy(reader, sp, dst, dp, tick, reader.stat(sp, follow), None, here)
    finally:
        if reader is not src:
            reader.close()


def keep(a):  # what a copy carries over, like cp -p: mode (no setuid) and modified time
    return {"perm": a.get("perm", 0o644) & 0o777} | ({"mtime": a["mtime"]} if "mtime" in a else {})


def _copy(src, sp, dst, dp, tick, a, there, here):
    name = dp.rsplit("/", 1)[-1]
    old = there.get(name) if there is not None else peek(dst, dp)
    if islink(a):
        if old and not isdir(old):
            dst.unlink(dp)
        return dst.symlink(a.get("target") or src.readlink(sp), dp)
    if special(a):
        raise OSError(f"{sp}: not a regular file")
    if isdir(a):
        try:
            dst.mkdir(dp)
            there = {}  # brand new folder: nothing in it to look up
        except ConnectionError:
            raise
        except OSError:
            if not isdir(dst.stat(dp)):
                raise
            there = dict(dst.listdir(dp))  # one listing instead of two lookups per file
        for n, b in sorted(src.listdir(sp), key=lambda x: x[0]):
            if special(b):
                tick.skipped.append(join(sp, n))
            else:
                _copy(src, join(sp, n), dst, join(dp, n), tick, b, there, here)
        try:  # the folder's own mode and time, once its content is in
            dst.chattr(dp, keep(a))
        except ConnectionError:
            raise
        except OSError:  # a folder not yours keeps its own
            pass
        return
    if sp in tick.finished:
        return
    if old and islink(old):  # write through a link, like cp: the link stays, its target gets the new content
        dp, there = dst.realpath(dp), None
    part = there.get(name + PART) if there is not None else peek(dst, dp + PART)
    size, off = a.get("size", 0), (part or {}).get("size", 0)
    n = min(off, CHUNK)  # resumed only if its last bytes are the source's: not a stray file, not an older version
    if off > size or n and b"".join(dst.read(dp + PART, off - n, off)) != b"".join(src.read(sp, off - n, off)):
        off = 0
    ch = keep(a)  # the source's mode and time, over a file there too: a copy is the source, not a blend
    tick(sp, off, resumed=True)
    try:
        if here:
            dst.sftp().copy_data(sp, dp + PART, off, ch)
            tick(sp, size)
        else:
            chunks = src.read(sp, off, size)
            try:  # the source open (readable) before anything is made over there
                first = list(itertools.islice(chunks, 1))
                dst.write(dp + PART, itertools.chain(first, chunks), off, ch, lambda at: tick(sp, at))
            finally:
                chunks.close()
        dst.rename(dp + PART, dp)  # the owner: whoever copies (ids differ from host to host), as rsync without root
    except KeyboardInterrupt:  # stopped by you: nothing of it stays there (a lost link keeps it, to resume)
        with contextlib.suppress(OSError):
            dst.unlink(dp + PART)
        raise
    tick.finished.add(sp)
