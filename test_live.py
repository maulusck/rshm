#!/usr/bin/env python3
"""Live check against a real, throwaway sshd, through a jump set up in an ssh config (as it should be): the real
screen in a terminal (open a host, copy a file over with the keys, a shell there), a tree both ways, the live check,
and nothing left behind.
Runs as you on a free local port, in a temp folder that is removed afterwards. No root, no users, no network."""
import contextlib, fcntl, hashlib, os, pty, select, shutil, socket, struct, subprocess, sys, tempfile, termios, time, zipapp

try:
    import pyte  # a terminal emulator to read the real screen with (pip install pyte)
except ImportError:
    sys.exit("(no pyte here: live test skipped)")
from pathlib import Path

sshd = shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.exists(p)), None)
sftp = next((p for p in ("/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/libexec/sftp-server",
                         "/usr/lib/ssh/sftp-server") if os.path.exists(p)), None)
if not (sshd and sftp):
    sys.exit("(no sshd/sftp-server here: live test skipped)")

T = Path(tempfile.mkdtemp(prefix="rshm-live-"))
os.environ["HOME"] = str(T)  # rshm's config, its sockets and the ssh config all land in here
from rshm import config, fs, ssh  # noqa: E402  (after HOME is set)

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
for k in ("host", "user"):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", T / k], check=True)
(T / ".ssh").mkdir(mode=0o700)
(T / "user.pub").rename(T / "authorized_keys")
(T / "sshd_config").write_text(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {T}/host
AuthorizedKeysFile {T}/authorized_keys
PidFile {T}/sshd.pid
StrictModes no
UsePAM no
PasswordAuthentication no
Subsystem sftp {sftp}
""")
srv = subprocess.Popen([sshd, "-D", "-e", "-f", T / "sshd_config"], stderr=subprocess.DEVNULL)
time.sleep(0.5)
me = os.environ.get("USER") or subprocess.run(["id", "-un"], stdout=subprocess.PIPE, text=True).stdout.strip()
# everything about reaching the hosts is the ssh config's: user, port, key, the jump
(T / ".ssh" / "config").write_text(f"""Host direct jumped
	User {me}
	Port {port}
	IdentityFile {T}/user
	UserKnownHostsFile {T}/known_hosts
	StrictHostKeyChecking accept-new
Host direct
	HostName 127.0.0.1
Host jumped
	HostName localhost
	ProxyJump direct
""")
# ssh reads the real user's ~/.ssh/config, not $HOME's: point it at this one (jump children get -F too)
(T / "bin").mkdir()
(T / "bin" / "ssh").write_text(f'#!/bin/sh\nexec {shutil.which("ssh")} -F {T}/.ssh/config "$@"\n')
(T / "bin" / "ssh").chmod(0o755)
os.environ["PATH"] = f"{T}/bin:{os.environ['PATH']}"
cfg = T / "rshm.toml"
cfg.write_text('[hosts]\nhere = ["direct", "jumped"]\n')
sockets = lambda: sorted(p.name for p in (T / ".ssh").glob("rshm-" + "[0-9a-f]" * 16))
try:
    # the single-file build, as you'd carry it on a USB stick: rshm.pyz with its rshm.toml beside it
    src = Path(__file__).resolve().parent
    zipapp.create_archive(src, T / "rshm.pyz", "/usr/bin/env python3", "rshm.__main__:main",
                          filter=lambda p: p.parts[0] == "rshm" and p.suffix == ".py")
    # the screen: `rshm jumped` opens it on the right (this machine on the left), through the jump
    L = T / "up"
    (L / "sub").mkdir(parents=True)
    data = os.urandom(3 * 1024 * 1024 + 7)
    (L / "big.bin").write_bytes(data)
    (L / "sub" / "x.sh").write_text("#!/bin/sh\n")
    os.chmod(L / "sub" / "x.sh", 0o750)
    (T / "remote").mkdir()
    screen, raw = pyte.Screen(120, 30), []
    stream = pyte.ByteStream(screen)
    pid, fd = pty.fork()
    if not pid:
        os.chdir(L)
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))  # the size pyte draws
        os.environ.update(TERM="xterm")
        for v in ("EDITOR", "VISUAL", "PAGER", "XDG_CONFIG_HOME", "PYTHONPATH"):
            os.environ.pop(v, None)
        os.execvp(sys.executable, [sys.executable, str(T / "rshm.pyz"), "jumped"])  # finds rshm.toml next to it

    def until(ok, what, wait=30):
        t0 = time.monotonic()
        while not ok("\n".join(screen.display)):
            assert time.monotonic() - t0 < wait, f"{what}:\n" + "\n".join(screen.display)
            if select.select([fd], [], [], 0.5)[0]:
                b = os.read(fd, 65536)
                raw.append(b)
                stream.feed(b)
    until(lambda s: "jumped:/" in s and "big.bin" in s, "both panes")
    assert len(sockets()) == 1, sockets()
    # copy with the keys: Tab to this machine, filter to the file, c (F5), a new destination, ⏎
    for k in (b"\t", b"/big", b"\r", b"c", b"\x15" + str(T / "remote").encode(), b"\r"):
        os.write(fd, k)
        time.sleep(0.2)
    until(lambda s: "✓ → big.bin" in s, "copied")
    assert (T / "remote" / "big.bin").read_bytes() == data
    # a shell on the host, in its folder, on the same login; exit brings the screen back
    os.write(fd, b"\ts")
    time.sleep(1)
    os.write(fd, b"echo RSHM_$((1+1)); exit\n")
    until(lambda s: b"RSHM_2" in b"".join(raw) and "F10 quit" in s and "RSHM_2" not in s, "shell, then the screen again")
    assert len(sockets()) == 1, sockets()
    os.write(fd, b"q")
    with contextlib.suppress(OSError):  # read on to the end: a full terminal would hold rshm's exit up
        while os.read(fd, 65536):
            pass
    assert os.waitpid(pid, 0)[1] == 0
    time.sleep(2)
    assert sockets() == [], f"the screen left logins behind: {sockets()}"

    # a tree up through the jump and back down, byte for byte, permissions kept; a copy within the host; the live check
    hs = config.hosts(config.read(cfg))
    h = config.find(hs, "jumped")[0][1]
    R, Lo = fs.Remote(h, term=lambda cmd: 1, say=lambda m: None), fs.Local()
    tick = lambda *a, **k: None
    tick.finished = set()
    fs.copy(Lo, str(L), R, str(T / "remote" / "t"), tick)
    tick.finished = set()
    fs.copy(R, str(T / "remote" / "t"), Lo, str(T / "down"), tick)
    tick.finished = set()
    fs.copy(R, str(T / "remote" / "t" / "big.bin"), R, str(T / "remote" / "same.bin"), tick)  # within one host
    fs.save(R, str(T / "remote" / "saved.txt"), b"over ssh\n")  # what the built-in editor's ^S does
    R.close()
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    assert sha(T / "down" / "big.bin") == sha(L / "big.bin") == sha(T / "remote" / "t" / "big.bin") == sha(T / "remote" / "same.bin")
    assert (T / "remote" / "saved.txt").read_bytes() == b"over ssh\n"
    assert (T / "down" / "sub" / "x.sh").stat().st_mode & 0o777 == 0o750 & ~os.umask(os.umask(0))
    assert not list(T.rglob("*" + fs.PART)) and len(sockets()) == 1
    assert ssh.quiet([*ssh.argv(h, batch=True, timeout=3), "jumped", "true"])[0] == 0

    # nothing left behind: the logins stop, their sockets go
    ssh.logout()
    time.sleep(2)
    assert sockets() == [], f"logins left behind: {sockets()}"
    print("live ok")
finally:
    srv.terminate()
    srv.wait()
    shutil.rmtree(T, ignore_errors=True)
