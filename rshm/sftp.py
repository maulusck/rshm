"""SFTP v3 (draft-ietf-secsh-filexfer-02), the version every server speaks, over `ssh -s host sftp`."""
import struct, subprocess, tempfile
from collections import deque

from .ssh import argv, last

CHUNK, WINDOW = 32768, 64  # what OpenSSH's sftp uses: 2 MB in flight, and every server accepts 32 KB


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

    def attrs(r):  # size, type/permissions, modified time; owners are skipped
        f, a = r.u32(), {}
        if f & 1:
            a["size"] = struct.unpack(">Q", r.take(8))[0]
        if f & 2:
            r.take(8)  # uid, gid
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


class SFTP:  # one session on the host's master: no remote shell, exact byte names, any SFTP server
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

    def alive(s):
        return s.p.poll() is None

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
                    n, _, a = r.str(), r.str(), r.attrs()
                    if n not in (b"", b".", b"..") and b"/" not in n:  # real entries only
                        out.append((n.decode(errors="surrogateescape"), a))
        finally:
            s.release(h)
        return out

    def mkdir(s, p):
        s.call(14, p, 0)

    def rmdir(s, p):
        s.call(15, p)

    def unlink(s, p):
        s.call(13, p)

    def chmod(s, p, mode):
        s.call(9, p, 4, mode)  # SETSTAT permissions

    def rename(s, a, b, replace=True):  # replace: atomically where the server can (OpenSSH), else remove then rename
        if not replace:
            return s.call(18, a, b)  # plain SFTP rename refuses to overwrite
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
        finally:
            s.release(h)

    def copy_data(s, a, b, off, mode):  # a -> b from off, copied by the server itself (OpenSSH 9.0+'s copy-data)
        ra = s.call(3, a, 1, 0).str()
        try:
            wb = s.call(3, b, 2 | 8 | (0x10 if off == 0 else 0), 0).str()
            try:
                s.call(200, "copy-data", ra, *off64(off), 0, 0, wb, *off64(off))  # length 0: to the end
                try:
                    s.call(10, wb, 4, mode)
                except OSError:
                    pass
            finally:
                s.release(wb)
        finally:
            s.release(ra)

    def write(s, p, chunks, off, mode, done):  # chunks -> p from off (0: truncate), then mode; done(at) as it lands
        h = s.call(3, p, 2 | 8 | (0x10 if off == 0 else 0), 0).str()  # WRITE | CREAT, TRUNC unless resuming
        try:
            q = deque()
            for chunk in chunks:
                q.append((s.req(6, h, *off64(off), chunk), off + len(chunk)))
                off += len(chunk)
                if len(q) == WINDOW:
                    i, end = q.popleft()
                    s.wait(i)
                    done(end)
            perm = s.req(10, h, 4, mode)  # FSETSTAT permissions: rides along with the last writes, no extra wait
            while q:
                i, end = q.popleft()
                s.wait(i)
                done(end)
            try:
                s.wait(perm)
            except OSError:
                pass  # a server that won't set permissions (some Windows builds) still gets the file
        finally:
            s.release(h)
