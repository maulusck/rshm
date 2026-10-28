"""SFTP v3 (draft-ietf-secsh-filexfer-02), the version every server speaks, over `ssh -s host sftp`."""
import errno, os, re, struct, subprocess, tempfile
from collections import deque

from .ssh import argv, last

CHUNK, WINDOW = 32768, 64  # what OpenSSH's sftp uses: 2 MB in flight, and every server accepts 32 KB


FX = {2: errno.ENOENT, 3: errno.EACCES, 8: errno.EOPNOTSUPP}  # SFTP v3 status -> errno


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
        if r.i > len(r.d):
            raise OSError("sftp: truncated packet")
        return r.d[r.i - n:r.i]

    def u32(r):
        return struct.unpack(">I", r.take(4))[0]

    def str(r):
        return r.take(r.u32())

    def attrs(r):  # size, owner ids, type/permissions, modified time
        f, a = r.u32(), {}
        if f & 1:
            a["size"] = struct.unpack(">Q", r.take(8))[0]
        if f & 2:
            a["uid"], a["gid"] = r.u32(), r.u32()
        if f & 4:
            a["perm"] = r.u32()
        if f & 8:
            r.u32()  # atime
            a["mtime"] = r.u32()
        if f & 0x80000000:
            for _ in range(2 * r.u32()):
                r.str()  # extended name/value pairs
        return a


def off64(o):
    return o >> 32, o & 0xFFFFFFFF


def packattrs(ch):  # {"uid", "gid", "perm", "mtime"} -> SFTP attributes: flags, then values (uid and gid as a pair)
    flags, args = 0, []
    if "uid" in ch:
        flags, args = 2, [ch["uid"], ch["gid"]]
    if "perm" in ch:
        flags, args = flags | 4, [*args, ch["perm"]]
    if "mtime" in ch:
        flags, args = flags | 8, [*args, ch["mtime"], ch["mtime"]]  # access time too, as cp -p does
    return flags, *args


class SFTP:  # one session on the host's master: no remote shell, exact byte names, any SFTP server
    def __init__(s, h):
        s.err = tempfile.TemporaryFile()  # a file, not a pipe: ssh can never block on a full stderr
        s.p = subprocess.Popen([*argv(h, batch=True), "-s", h["host"], "sftp"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=s.err,
                               start_new_session=True)  # ^C is for rshm: stop the transfer, don't kill the link
        s.n, s.early, s.ext, s.dropped = 0, {}, set(), set()  # dropped: requests whose replies nobody waits for

    def init(s):
        try:
            s.send(1, pack(3))  # INIT v3 -> VERSION + the extensions the server offers
            r = s.recv()[1]
        except BaseException:  # ^C while it starts: no half-started ssh left over
            s.close()
            raise
        r.u32()
        while r.i < len(r.d):
            s.ext.add(r.str())
            r.str()
        return s

    def alive(s):
        return s.p.poll() is None

    def close(s):
        s.p.kill()
        s.p.wait()

    def fail(s, why=None):  # the session is gone: a lost link, never an answer about a file
        s.close()
        raise ConnectionError(why or last(s.err) or "connection closed")

    def send(s, t, data):
        try:
            s.p.stdin.write(struct.pack(">IB", len(data) + 1, t) + data)
            s.p.stdin.flush()
        except OSError:
            s.fail()

    def read_exact(s, n):
        b = s.p.stdout.read(n)
        if len(b) < n:
            s.fail()
        return b

    def recv(s):
        head = s.read_exact(4)
        n = struct.unpack(">I", head)[0]
        if n > 1 << 24:  # not a packet: the login shell printed text first (sftp-server starts through it)
            s.fail(f"server printed {head + s.p.stdout.read1(40)!r}… before SFTP: a startup file like .bashrc echoes")
        body = s.read_exact(n)
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
                n = r.u32()
                if n in s.dropped:
                    s.dropped.discard(n)
                else:
                    s.early[n] = rt, r
        except BaseException:  # Ctrl-C or a dead link mid-packet: the stream is out of sync, drop it
            s.close()
            raise
        rt, r = s.early.pop(i)
        if rt != 101:  # anything but STATUS
            return r
        code = r.u32()
        if code in FX:  # as this machine says it: one wording for every error, here or there
            raise OSError(FX[code], os.strerror(FX[code]))
        if code > 1:  # 4, FAILURE: what OpenSSH answers for every other errno, reason unsaid
            raise OSError("failed on the server (SFTP gives no reason: disk full, quota, read-only?)" if code == 4
                          else r.str().decode(errors="replace") or f"SFTP status {code}")

    def forget(s, ids):  # requests nobody will wait for: their replies, here or still coming, are dropped
        for i in ids:
            if s.early.pop(i, None) is None:
                s.dropped.add(i)

    def call(s, t, *args):
        return s.wait(s.req(t, *args))

    def release(s, h):  # CLOSE a handle, unless the session already died (^C, lost link): that error must win
        if s.alive():
            s.call(4, h)

    # --- the file system primitives (see fs.py) ---
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
                    n, long, a = r.str(), r.str().split(), r.attrs()
                    if len(long) > 3 and re.fullmatch(rb"[-dlcbps][-rwxsStT]{9}[.+@]?", long[0]):  # `ls -l`: more
                        a["user"], a["group"] = long[2].decode(errors="replace"), long[3].decode(errors="replace")
                        a["nlink"] = int(long[1]) if long[1].isdigit() else None
                    if n not in (b"", b".", b"..") and b"/" not in n:  # real entries only
                        out.append((n.decode(errors="surrogateescape"), a))
        finally:
            s.release(h)
        return out

    def make(s, p, t, *args):  # a request that creates p: SFTP v3 says just "Failure" when p is there; say that
        try:
            return s.call(t, *args)
        except OSError as e:
            if isinstance(e, ConnectionError) or not s.there(p):
                raise
        raise FileExistsError(f"{p}: already exists")

    def there(s, p):
        try:
            return s.stat(p, follow=False) is not None
        except ConnectionError:
            raise
        except OSError:
            return False

    def mkdir(s, p):
        s.make(p, 14, p, 0)

    def rmdir(s, p):
        s.call(15, p)

    def unlink(s, p):
        s.call(13, p)

    def chattr(s, p, ch):  # SETSTAT; one of uid/gid alone gets its partner from the file
        if ("uid" in ch) != ("gid" in ch):
            ch = {**{k: v for k, v in s.stat(p).items() if k in ("uid", "gid")}, **ch}
        s.call(9, p, *packattrs(ch))

    def readlink(s, p):
        r = s.call(19, p)
        r.u32()
        return r.str().decode(errors="surrogateescape")

    def symlink(s, target, p):  # ponytail: OpenSSH's order, not the draft's; check the server's if another shows up
        s.make(p, 20, target, p)

    def hardlink(s, src, p):
        if b"hardlink@openssh.com" not in s.ext:
            raise OSError("server does not support hard links")
        s.make(p, 200, "hardlink@openssh.com", src, p)

    def free(s, p):  # bytes free for you there, None if the server won't say
        if b"statvfs@openssh.com" not in s.ext:
            return None
        r = s.call(200, "statvfs@openssh.com", p)
        v = struct.unpack(">5Q", r.take(40))  # bsize, frsize, blocks, bfree, bavail
        return v[1] * v[4]

    def rename(s, a, b, replace=True):  # replace: atomically where the server can (OpenSSH), else remove then rename
        if not replace:
            return s.make(b, 18, a, b)  # plain SFTP rename refuses to overwrite
        if b"posix-rename@openssh.com" in s.ext:
            return s.call(200, "posix-rename@openssh.com", a, b)
        try:
            s.unlink(b)
        except OSError:
            pass
        s.call(18, a, b)

    def read(s, p, off, size):  # -> chunks of p from off to size, WINDOW requests in flight
        h = s.call(3, p, 1, 0).str()  # OPEN for READ
        try:
            q = deque()
            while off < size or q:
                while off < size and len(q) < WINDOW:
                    n = min(CHUNK, size - off)
                    q.append((s.req(5, h, *off64(off), n), off, n))
                    off += n
                i, o, n = q.popleft()
                data = (r := s.wait(i)) and r.str() or b""
                while len(data) < n:  # short read: fetch the rest before handing it on
                    more = (r := s.call(5, h, *off64(o + len(data)), n - len(data))) and r.str()
                    if not more:
                        raise OSError(f"{p}: file shrank while being read")
                    data += more
                yield data
        finally:  # stopped early (the writer failed, ^C): the replies still on their way are nobody's
            s.forget(i for i, _, _ in q)
            s.release(h)

    def copy_data(s, a, b, off, ch):  # a -> b from off, copied by the server itself (OpenSSH 9.0+'s copy-data)
        ra = s.call(3, a, 1, 0).str()
        try:
            wb = s.call(3, b, 2 | 8 | (0x10 if off == 0 else 0), 0).str()
            try:
                s.call(200, "copy-data", ra, *off64(off), 0, 0, wb, *off64(off))  # length 0: to the end
                try:
                    s.call(10, wb, *packattrs(ch))
                except ConnectionError:
                    raise
                except OSError:  # a server that won't set them still gets the file
                    pass
            finally:
                s.release(wb)
        finally:
            s.release(ra)

    def write(s, p, chunks, off, ch, done):  # chunks -> p from off (0: truncate), then attributes ch; done(at) as it lands
        h = s.call(3, p, 2 | 8 | (0x10 if off == 0 else 0), 0).str()  # WRITE | CREAT, TRUNC unless resuming
        q, perm = deque(), None
        try:
            for chunk in chunks:
                q.append((s.req(6, h, *off64(off), chunk), off + len(chunk)))
                off += len(chunk)
                if len(q) == WINDOW:
                    i, end = q.popleft()
                    s.wait(i)
                    done(end)
            perm = s.req(10, h, *packattrs(ch))  # FSETSTAT: rides along with the last writes, no extra wait
            while q:
                i, end = q.popleft()
                s.wait(i)
                done(end)
            try:
                s.wait(perm)
            except ConnectionError:
                raise
            except OSError:  # a server that won't set permissions (some Windows builds) still gets the file
                pass
        finally:
            s.forget([*(i for i, _ in q), *([perm] if perm else [])])
            s.release(h)
