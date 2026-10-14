"""One file system interface, three of them: Local (this machine), Remote (a host, over SFTP) and Archive (a zip or
tar, read-only). Everything a pane does goes through these primitives, so copy, move and delete work between any two.

    name                                its label
    realpath(p) ("" = home) stat(p, follow) listdir(d) mkdir(p) rmdir(p) unlink(p) rename(a, b, replace)
    chattr(p, changes)                  changes: {"perm", "uid", "gid"}; the one door for attributes (ACLs and
                                        SELinux labels will come through it too)
    attrs: {"size", "perm", "mtime", "uid", "gid", "user", "group"}, each only when known
    read(p, off, size) -> chunks        write(p, chunks, off, mode, done)
    run(script, d) -> (argv, cwd)       a shell command in folder d, on the terminal
    alive()                             False once a session dropped (a transfer then reconnects and resumes)
    close()
"""
import functools, grp, os, posixpath, pwd, re, shlex, stat, tarfile, time, zipfile
from collections import defaultdict

from .sftp import CHUNK, SFTP
from .ssh import argv, connect

PART = ".rshm-part"  # a file in transit: resumed from, renamed into place once complete
ARCHIVES = (".zip", ".jar", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tar.bz2", ".txz", ".tar.xz")


def isdir(a):
    return stat.S_ISDIR(a.get("perm", 0))


def islink(a):
    return stat.S_ISLNK(a.get("perm", 0))


def special(a):  # pipe, socket or device: opening one could block forever, so transfers never do
    return stat.S_IFMT(a.get("perm", 0)) in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK)


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
            "user": user, "group": group}


class Local:
    name = "local"

    def realpath(s, p):
        return os.path.realpath(os.path.expanduser(p or "~"))

    def stat(s, p, follow=True):
        return attrs(os.stat(p) if follow else os.lstat(p))

    def listdir(s, d):
        with os.scandir(d) as it:
            return [(e.name, attrs(e.stat(follow_symlinks=False))) for e in it]

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

    def chattr(s, p, ch):
        if "uid" in ch or "gid" in ch:
            os.chown(p, ch.get("uid", -1), ch.get("gid", -1))
        if "perm" in ch:  # after the owner: chown may drop setuid bits
            os.chmod(p, ch["perm"])

    def read(s, p, off, size):
        with open(p, "rb") as f:
            f.seek(off)
            while off < size:
                b = f.read(min(CHUNK, size - off))
                if not b:
                    raise OSError(f"{p}: file shrank while being read")
                off += len(b)
                yield b

    def write(s, p, chunks, off, mode, done):  # new files get mode (less umask), owner-writable so a resume can append
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | (0 if off else os.O_TRUNC), mode | 0o200)
        with open(fd, "wb") as f:
            f.seek(off)
            for b in chunks:
                f.write(b)
                off += len(b)
                done(off)

    def run(s, script, d):
        return ["sh", "-c", script], d

    def alive(s):
        return True

    def close(s):
        pass


class Remote:  # a host; its SFTP session opens on first use and reopens after a drop
    PRIMS = {"stat", "listdir", "mkdir", "rmdir", "unlink", "rename", "chattr", "read", "write"}

    def __init__(s, h, term, say):
        s.h, s.term, s.say, s.sf, s.name = h, term, say, None, h["host"]

    def sftp(s):
        if not (s.sf and s.sf.alive()):
            s.sf = None  # until the new session is up: a failed login is a refusal, not a dropped link
            s.login()
            s.sf = SFTP(s.h).init()
        return s.sf

    def login(s):  # the host's master, logged in (a password or host key may need the terminal)
        connect(s.h, s.term, s.say)

    def __getattr__(s, n):  # the primitives are the session's own
        if n not in Remote.PRIMS:
            raise AttributeError(n)
        return getattr(s.sftp(), n)

    def realpath(s, p):
        return s.sftp().realpath(re.sub(r"^~(/|$)", "", p) or ".")  # sftp starts in $HOME, so ~/x is just x

    def run(s, script, d):  # ponytail: `cd` needs a POSIX login shell; browsing and transfers need none
        s.login()
        return [*argv(s.h), "-t", s.h["host"], f"cd {shlex.quote(d)} && {script}" if d else script], None

    def alive(s):
        return s.sf is None or s.sf.alive()

    def close(s):
        if s.sf:
            s.sf.close()

    def twin(s):  # a second session to the same host, on the same login
        return Remote(s.h, s.term, s.say)


class Archive:  # a zip or tar file (gz, bz2, xz), browsed read-only like a folder; Python reads them all itself
    def __init__(s, path, name):
        s.name, s.files = name, {"/": {"perm": stat.S_IFDIR | 0o755, "size": 0}}  # every entry by its path inside
        try:
            if zipfile.is_zipfile(path):
                s.z = zipfile.ZipFile(path)
                for i in s.z.infolist():
                    kind = stat.S_IFDIR if i.is_dir() else stat.S_IFREG
                    s.files["/" + i.filename.strip("/")] = {"size": i.file_size, "ref": i, "perm": i.external_attr >> 16
                                                            or kind | 0o644, "mtime": int(time.mktime((*i.date_time, 0, 0, -1)))}
            else:
                s.z = tarfile.open(path)
                for m in s.z.getmembers():
                    kind = stat.S_IFDIR if m.isdir() else stat.S_IFLNK if m.issym() else stat.S_IFREG if m.isfile() \
                        or m.islnk() else stat.S_IFIFO
                    s.files["/" + m.name.strip("/")] = {"size": m.size, "ref": m, "perm": kind | m.mode, "mtime": m.mtime,
                                                        "uid": m.uid, "gid": m.gid, "user": m.uname, "group": m.gname}
        except (tarfile.TarError, zipfile.BadZipFile, EOFError) as e:
            raise OSError(f"{name}: not an archive I can read ({e})") from None
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
        a = s.files.get(s.realpath(p))
        if a is None or follow and islink(a):  # ponytail: links inside an archive aren't followed
            raise FileNotFoundError(f"{p}: not in {s.name}")
        return a

    def listdir(s, d):
        return list(s.kids[s.realpath(d)])

    def read(s, p, off, size):
        ref = s.stat(p)["ref"]
        with (s.z.open(ref) if isinstance(s.z, zipfile.ZipFile) else s.z.extractfile(ref)) as f:
            f.seek(off)
            while off < size:
                b = f.read(min(CHUNK, size - off))
                if not b:
                    raise OSError(f"{p}: shorter than the archive says")
                off += len(b)
                yield b

    def readonly(s, *_, **__):
        raise OSError(f"{s.name} is an archive: read-only")

    mkdir = rmdir = unlink = rename = chattr = write = run = readonly

    def alive(s):
        return True

    def close(s):
        s.z.close()


def peek(fs, p):  # attrs of whatever is at p (links not followed), None if nothing is
    try:
        return fs.stat(p, follow=False)
    except OSError:
        return None


def rm(fs, p, a=None):  # recursive; never follows a symlink out of the tree
    if isdir(a or fs.stat(p, follow=False)):
        for n, a in fs.listdir(p):
            rm(fs, join(p, n), a)
        fs.rmdir(p)
    else:
        fs.unlink(p)


def save(fs, p, data):  # p's content replaced at once (never half-written), its permissions kept; a link written through
    old = peek(fs, p)
    if old and islink(old):
        p = fs.realpath(p)
        old = fs.stat(p)
    fs.write(p + PART, (data[i:i + CHUNK] for i in range(0, len(data), CHUNK)), 0,
             (old or {}).get("perm", 0o644) & 0o777, lambda at: None)
    fs.rename(p + PART, p)


def copy(src, sp, dst, dp, tick, a=None):
    """src:sp -> dst:dp, a file or a tree. Each file lands as NAME.rshm-part, resumed if one is there, renamed into place
    when complete; replacing a file keeps its permissions. tick(path, at, resumed) reports progress; tick.finished
    holds what already made it, so a retry after a dropped link skips it. Within one host the server copies by
    itself where it can (OpenSSH 9.0+), else a second session reads: one session's reads in flight would block its writes."""
    one = src is dst and isinstance(src, Remote)
    here = one and b"copy-data" in src.sftp().ext
    reader = src.twin() if one and not here else src
    try:
        _copy(reader, sp, dst, dp, tick, a, None, here)
    finally:
        if reader is not src:
            reader.close()


def _copy(src, sp, dst, dp, tick, a, there, here):
    a = a or src.stat(sp)
    if special(a):
        raise OSError(f"{sp}: not a regular file")
    if isdir(a):
        try:
            dst.mkdir(dp)
            there = {}  # brand new folder: nothing in it to look up
        except OSError:
            if not isdir(dst.stat(dp)):
                raise
            there = dict(dst.listdir(dp))  # one listing instead of two lookups per file
        for n, a in sorted(src.listdir(sp), key=lambda x: x[0]):
            if islink(a):
                try:
                    a = src.stat(join(sp, n))
                except OSError:
                    continue  # dangling
                if isdir(a):
                    continue  # ponytail: linked folders inside a tree are skipped, so a link loop can't recurse
            if not special(a):
                _copy(src, join(sp, n), dst, join(dp, n), tick, a, there, here)
        return
    if sp in tick.finished:
        return
    name = dp.rsplit("/", 1)[-1]
    old = there.get(name) if there is not None else peek(dst, dp)
    if old and islink(old):  # write through a link, like cp: the link stays, its target gets the new content
        dp, there = dst.realpath(dp), None
        old = dst.stat(dp)
    part = there.get(name + PART) if there is not None else peek(dst, dp + PART)
    size, off = a.get("size", 0), (part or {}).get("size", 0)
    off = 0 if off > size else off  # bigger than the source: not a partial of it, start over
    mode = ((old or a).get("perm") or 0o644) & 0o777
    tick(sp, off, resumed=True)
    if here:
        dst.sftp().copy_data(sp, dp + PART, off, mode)
        tick(sp, size)
    else:
        chunks = src.read(sp, off, size)
        try:
            dst.write(dp + PART, chunks, off, mode, lambda at: tick(sp, at))
        finally:
            chunks.close()
    dst.rename(dp + PART, dp)  # ponytail: a replaced file now belongs to whoever copied it
    tick.finished.add(sp)
