"""Logins: one OpenSSH master per destination, reused by everything; stopped when rshm exits.
Everything about reaching a host (user, key, jump, password) is ~/.ssh/config's business, never rshm's."""
import hashlib, os, re, subprocess, tempfile
from pathlib import Path


def argv(h, batch=False, timeout=8):  # ssh keeps the FIRST value of an option: these win over ~/.ssh/config
    o = {"ControlMaster": "auto", "ControlPersist": 300, "ConnectTimeout": timeout,
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


def connect(h, term, say, timeout=8):  # make sure h's master is up and logged in; term(cmd) runs cmd on the terminal
    if quiet([*argv(h), "-O", "check", h["host"]])[0] == 0:
        return
    say(f"connecting {h['host']}…")
    (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)  # where the login sockets live
    # quietly first: keys, the agent, or your own SSH_ASKPASS answer without a terminal (one try at a password)
    rc, err = quiet([*argv(h, batch=not os.environ.get("SSH_ASKPASS"), timeout=timeout),
                     "-o", "NumberOfPasswordPrompts=1", "-N", h["host"]])  # ControlPersist: backgrounds once in
    if rc and (unreachable(err) or term([*argv(h, timeout=timeout), "-N", h["host"]]) != 0):
        # else ssh asks in the terminal (new host key, 2FA, a password); a dead network won't take a typed one
        raise ConnectionError(err if err.startswith(h["host"]) else f"{h['host']}: {err}")
    LOGINS.append(h)
