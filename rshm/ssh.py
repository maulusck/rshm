"""Logins: one OpenSSH master per destination, reused by everything; stopped when rshm exits.
Everything about reaching a host (user, key, jump, password) is ~/.ssh/config's business, never rshm's."""
import hashlib, os, re, signal, subprocess, tempfile
from pathlib import Path


def argv(h, batch=False, timeout=8):  # ssh keeps the FIRST value of an option: these win over ~/.ssh/config
    o = {"ControlMaster": "auto", "ControlPersist": 300, **({"ConnectTimeout": timeout} if timeout else {}),
         "ServerAliveInterval": 15,  # a dead link errors out in ~45 s, never hangs
         # a short fixed name, not %C: a unix socket path must fit in ~104 bytes, even under Termux's long $HOME
         "ControlPath": Path.home() / ".ssh" / f"rshm-{hashlib.sha1(h['host'].encode()).hexdigest()[:16]}"}
    if batch:
        o["BatchMode"] = "yes"
    return ["ssh", *(x for k, v in o.items() for x in ("-o", f"{k}={v}"))]


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


def login(cmd):  # ssh on the terminal, in its own process group put in the foreground -> (exit code, last stderr line)
    # it, its ProxyCommand (sudo in it) and pinentry can ask and take ^C; once in, the group is in the background again,
    # so rshm's own ^C (stop a transfer) never reaches the tunnel it leaves behind
    with tempfile.TemporaryFile() as err:  # not the screen: a jump's ssh -W scribbling on it later
        try:
            fd = os.open("/dev/tty", os.O_RDWR)
        except OSError:  # no terminal: nobody to ask
            return quiet(cmd)
        ttou = signal.signal(signal.SIGTTOU, signal.SIG_IGN)  # a background group may take the terminal back

        def front():
            os.setpgid(0, 0)
            os.tcsetpgrp(fd, os.getpgrp())
            signal.signal(signal.SIGTTOU, signal.SIG_DFL)

        try:
            rc = subprocess.call(cmd, stderr=err, preexec_fn=front)
        finally:
            os.tcsetpgrp(fd, os.getpgrp())
            signal.signal(signal.SIGTTOU, ttou)
            os.close(fd)
        return rc, last(err) or f"exit {rc}"


def connect(h, tty, say, timeout=None, ask=True):  # h's master up and logged in; tty(fn, *a) runs fn with the terminal
    if quiet([*argv(h), "-O", "check", h["host"]])[0] == 0:
        return
    say(f"connecting {h['host']}…")
    (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)  # where the login sockets live
    # one try, terminal handed over (a gpg passphrase asks even in batch mode); ControlPersist: backgrounds once in.
    # no ConnectTimeout of ours unless asked: a ProxyCommand may take its time (wake a VM), ~/.ssh/config decides;
    # ponytail: a blackholed route waits for TCP's own timeout (~2 min) or ^C
    cmd = [*argv(h, batch=not ask, timeout=timeout), "-N", h["host"]]
    rc, err = tty(login if ask else quiet, cmd)
    if rc:
        raise ConnectionError(err if err.startswith(h["host"]) else f"{h['host']}: {err}")
    LOGINS.append(h)
