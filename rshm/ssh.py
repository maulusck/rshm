"""Logins: one OpenSSH master per destination, reused by everything; stopped when rshm exits.
Everything about reaching a host (user, key, jump, password) is ~/.ssh/config's business, never rshm's."""
import hashlib, os, re, signal, subprocess, tempfile, threading
from pathlib import Path


def argv(h, batch=False, timeout=8):  # ssh keeps the FIRST value of an option: these win over ~/.ssh/config
    o = {"ControlMaster": "auto", "ControlPersist": 300, **({"ConnectTimeout": timeout} if timeout else {}),
         "ServerAliveInterval": 15,  # a dead link errors out in ~45 s, never hangs
         # a short fixed name, not %C: a unix socket path must fit in ~104 bytes, even under Termux's long $HOME
         "ControlPath": Path.home() / ".ssh" / f"rshm-{hashlib.sha1(h['host'].encode()).hexdigest()[:16]}"}
    if batch:
        o["BatchMode"] = "yes"
    return ["ssh", *(x for k, v in o.items() for x in ("-o", f"{k}={v}"))]


def lines(f):  # the lines written to a stderr file: what ssh had to say, a banner's colours and controls left out
    f.seek(0)
    text = re.sub(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)?|.)", "", f.read().decode(errors="replace"))
    return [ln for ln in (" ".join(re.sub(r"[\x00-\x1f\x7f]", " ", x).split()) for x in text.splitlines()) if ln]


def last(f):
    return (lines(f) or [""])[-1]


def told(f, rc):  # what ssh said, in a line: why it failed (its last), else its banner's headline (its first), if any
    said = lines(f)
    return (said[-1] if said else f"exit {rc}") if rc else (said or [""])[0]


def quiet(cmd):  # run detached from any terminal -> (exit code, last stderr line)
    with tempfile.TemporaryFile() as err:  # a file, not a pipe: a master forking off with it can't hang us
        # own session: masters and their jump tunnels outlive whatever terminal rshm runs in
        rc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                            start_new_session=True).returncode
        return rc, told(err, rc)


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

        done = threading.Event()

        def echo():  # what ssh and its ProxyCommand say while you log in (banner, "waking up...", the server's
            at = 0   # instructions), shown as it comes, as plain ssh shows it; nothing after: the screen is rshm's again
            while True:
                b = os.pread(err.fileno(), 65536, at)
                if b:
                    at += os.write(fd, b)
                elif done.wait(0.05):
                    return
        show = threading.Thread(target=echo, daemon=True)
        show.start()
        try:
            rc = subprocess.call(cmd, stderr=err, preexec_fn=front)
        finally:
            done.set()
            show.join()
            os.tcsetpgrp(fd, os.getpgrp())
            signal.signal(signal.SIGTTOU, ttou)
            os.close(fd)
        return rc, told(err, rc)


def connect(h, tty, say, timeout=None, ask=True):  # h's master up and logged in -> what the login said (a banner's
    if quiet([*argv(h), "-O", "check", h["host"]])[0] == 0:  # headline, "" if nothing); tty(fn, *a) runs fn with the
        return ""                                            # terminal
    say(f"connecting {h['host']}…")
    (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)  # where the login sockets live
    # one try, terminal handed over (a gpg passphrase asks even in batch mode); -f: backgrounds once in. Not left to
    # ControlPersist: OpenSSH 8.4 (Debian 11) then reconnects as a client that ignores -N and opens a shell on the
    # host, reading our terminal: the login never returns
    # no ConnectTimeout of ours unless asked: a ProxyCommand may take its time (wake a VM), ~/.ssh/config decides;
    # ponytail: a blackholed route waits for TCP's own timeout (~2 min) or ^C
    cmd = [*argv(h, batch=not ask, timeout=timeout), "-f", "-N", h["host"]]
    rc, err = tty(login if ask else quiet, cmd)
    if rc:
        raise ConnectionError(err if err.startswith(h["host"]) else f"{h['host']}: {err}")
    LOGINS.append(h)
    return err
