#!/usr/bin/env python3
"""Live check against a real, throwaway sshd: a login through a jump (set up in an ssh config, as it should be),
the live check, SFTP both ways, the CLI, and nothing left behind.
Runs as you on a free local port, in a temp folder that is removed afterwards. No root, no users, no network."""
import hashlib, os, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path

sshd = shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.exists(p)), None)
sftp = next((p for p in ("/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/libexec/sftp-server",
                         "/usr/lib/ssh/sftp-server") if os.path.exists(p)), None)
if not (sshd and sftp):
    sys.exit("(no sshd/sftp-server here: live test skipped)")

T = Path(tempfile.mkdtemp(prefix="rshm-live-"))
os.environ["HOME"] = str(T)  # rshm's config, its sockets and the ssh config all land in here
import rshm  # noqa: E402  (after HOME is set)

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
    # the CLI: logs in through the jump, runs ssh, and stops its logins on the way out
    r = subprocess.run(["rshm", "-c", cfg, "jumped"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=60) if shutil.which("rshm") else None
    if r:
        assert r.returncode == 0, r.stderr
        time.sleep(2)
        assert sockets() == [], f"CLI left logins behind: {sockets()}"

    # login through the jump (the ssh config's ProxyJump), no terminal needed; then the live check: a round trip
    hs = rshm.hosts(rshm.read(cfg))
    h = rshm.find(hs, "jumped")[0][1]
    rshm.connect(h, term=lambda cmd: 1, say=lambda m: None)
    assert len(sockets()) == 1, sockets()
    assert rshm.quiet([*rshm.argv(h, batch=True, timeout=3), "jumped", "true"])[0] == 0

    # SFTP through the jump: a tree up, the same tree down, byte for byte, permissions kept
    L, R = T / "up", T / "remote"
    (L / "sub").mkdir(parents=True)
    data = os.urandom(3 * 1024 * 1024 + 7)
    (L / "big.bin").write_bytes(data)
    (L / "sub" / "x.sh").write_text("#!/bin/sh\n")
    os.chmod(L / "sub" / "x.sh", 0o750)
    tick = lambda *a, **k: None
    tick.finished = set()
    s = rshm.SFTP(h).init()
    rshm.send(s, str(L), str(R), tick)
    tick.finished = set()
    rshm.fetch(s, str(R), str(T / "down"), tick)
    s.close()
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    assert sha(T / "down" / "big.bin") == sha(L / "big.bin") == sha(R / "big.bin")
    assert (T / "down" / "sub" / "x.sh").stat().st_mode & 0o777 == 0o750 & ~os.umask(os.umask(0))
    assert not list(T.rglob("*" + rshm.PART))

    # nothing left behind: the logins stop, their sockets go
    rshm.logout()
    time.sleep(2)
    assert sockets() == [], f"logins left behind: {sockets()}"
    print("live ok" + ("" if r else " (rshm not installed: CLI step skipped)"))
finally:
    srv.terminate()
    srv.wait()
    shutil.rmtree(T, ignore_errors=True)
