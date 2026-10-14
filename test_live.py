#!/usr/bin/env python3
"""rshm for real: the single file it builds of itself, on a real screen in a terminal (read back with pyte).
1. this machine: every key, each effect checked on screen or on disk; the layouts as the terminal turns and shrinks
2. hosts (when sshd is here): a throwaway sshd, reached through a jump set up in an ssh config as it should be;
   open, copy with the keys, a shell, a tree both ways, a copy within the host, the live check; nothing left behind
Runs as you, in a temp folder removed afterwards, sshd on a free local port. No root, no network, no other users."""
import contextlib, fcntl, hashlib, os, pty, select, shutil, signal, socket, struct, subprocess, sys, tempfile, termios, time
from pathlib import Path

try:
    import pyte  # a terminal emulator to read the real screen with (pip install pyte)
except ImportError:
    sys.exit("(no pyte here: live test skipped)")

T = Path(tempfile.mkdtemp(prefix="rshm-live-"))
os.environ["HOME"] = str(T)  # rshm's config, its sockets and the ssh config all land in here
for v in ("EDITOR", "VISUAL", "PAGER", "XDG_CONFIG_HOME"):
    os.environ.pop(v, None)  # rshm's own viewer and editor
from rshm import config, fs, ssh  # noqa: E402  (after HOME is set)

class Screen(pyte.Screen):  # pyte lacks the scroll curses uses on xterm (CSI n S / T): what a terminal does
    def scroll(s, n, up):
        y, (top, bottom) = s.cursor.y, s.margins or (0, s.lines - 1)
        for _ in range(n or 1):
            s.cursor.y = bottom if up else top
            s.index() if up else s.reverse_index()
        s.cursor.y = y

    def scroll_up(s, n=1, *_, **__):
        s.scroll(n, True)

    def scroll_down(s, n=1, *_, **__):
        s.scroll(n, False)


class Stream(pyte.ByteStream):
    csi = {**pyte.ByteStream.csi, "S": "scroll_up", "T": "scroll_down"}


KEY = {"F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS", "F5": "\x1b[15~", "F6": "\x1b[17~",
       "F7": "\x1b[18~", "F8": "\x1b[19~", "F9": "\x1b[20~", "F10": "\x1b[21~", "Esc": "\x1b", "Tab": "\t", "Enter": "\r",
       "Down": "\x1bOB", "^O": "\x0f", "^Q": "\x11", "^S": "\x13", "^U": "\x15"}  # as xterm sends them


class Term:  # one rshm in a pseudo terminal, its screen kept by pyte
    def __init__(t, args, cwd, rows=30, cols=120):
        t.screen, t.raw = Screen(cols, rows), []
        t.stream = Stream(t.screen)
        t.pid, t.fd = pty.fork()
        if not t.pid:
            os.chdir(cwd)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.environ["TERM"] = "xterm"
            os.environ.pop("PYTHONPATH", None)
            os.execvp(sys.executable, [sys.executable, str(T / "rshm.pyz"), *args])  # finds rshm.toml beside it

    def text(t):
        return "\n".join(t.screen.display)

    def pump(t, wait=0.15):
        while select.select([t.fd], [], [], wait)[0]:
            b = os.read(t.fd, 65536)
            t.raw.append(b)
            t.stream.feed(b)
            wait = 0.05

    def until(t, ok, what, wait=20):
        t0 = time.monotonic()
        while not ok(t.text()):
            assert time.monotonic() - t0 < wait, f"{what}:\n{t.text()}"
            t.pump(0.3)

    def keys(t, *ks):  # names from KEY, or text typed as is
        for k in ks:
            os.write(t.fd, KEY.get(k, k).encode())
            t.pump()

    def resize(t, rows, cols):
        fcntl.ioctl(t.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        t.screen.resize(rows, cols)
        os.kill(t.pid, signal.SIGWINCH)
        t.pump(0.3)

    def click(t, y, x):  # xterm mouse (SGR, as this terminfo asks): press and release at a cell
        os.write(t.fd, f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m".encode())
        t.pump()

    def quit(t):
        os.write(t.fd, b"q")
        with contextlib.suppress(OSError):  # read on to the end: a full terminal would hold rshm's exit up
            while os.read(t.fd, 65536):
                pass
        return os.waitpid(t.pid, 0)[1]


log = lambda s: s.split("└─")[-1]  # the activity panel and the key bar
try:
    # the single file, built by rshm itself, with its config beside it, as you'd carry it on a USB stick
    subprocess.run([sys.executable, "-m", "rshm", "--pyz", T / "rshm.pyz"], cwd=Path(__file__).resolve().parent, check=True)
    (T / "rshm.toml").write_text('theme = "default"\n[open]\n"*.marker" = "touch {file}.opened"\n'
                                 '[commands]\n"say hi" = "echo hi {names} > {dir}/menu.out"\n'
                                 '[hosts]\nhere = ["direct", "jumped"]\n')

    # --- 1. this machine, every key ---
    B, D = T / "box", T / "dst"
    (B / "sub").mkdir(parents=True)
    D.mkdir()
    (B / "note.txt").write_text("hello\nworld\n")
    (B / "mv.txt").write_text("move me")
    (B / ".dot").write_text("hidden")
    (B / "m.marker").write_text("")
    with __import__("zipfile").ZipFile(B / "data.zip", "w") as z:
        z.writestr("inside/a.txt", "A" * 300)
    t = Term([], B)
    t.until(lambda s: "local:" in s and "note.txt" in s and "hosts" in s, "this machine left, the host list right")
    t.keys("Tab", "Enter", "g", "^U", str(D), "Enter", "Tab")  # right pane: this machine too, in dst
    t.until(lambda s: f"local:{D}" in s, "right pane in dst")

    t.keys("/note", "Enter", "F3")  # view: text, search, hex, back
    t.until(lambda s: "hello" in s and "/ find" in s, "viewer")
    t.keys("/wor", "Enter")
    t.until(lambda s: t.screen.buffer[1][0].reverse and not t.screen.buffer[0][0].reverse, "viewer search: the match lit")
    t.keys("x")
    t.until(lambda s: "68 65 6c 6c 6f" in s, "hex")
    t.keys("q", "F4", "X", "^S", "^Q")  # edit: type, save, quit
    t.until(lambda s: "F10 quit" in s, "back from the editor")
    assert (B / "note.txt").read_text() == "Xhello\nworld\n"

    t.keys("A", "^U", "600", "Enter")  # attributes: the mode
    assert (B / "note.txt").stat().st_mode & 0o777 == 0o600, oct((B / "note.txt").stat().st_mode)
    t.until(lambda s: "-rw------- " in log(s), "ls -l line of the entry under the cursor")
    t.keys("c", "Enter")  # F5 copy to the other pane
    t.until(lambda s: "✓ → note.txt" in s, "copied")
    assert (D / "note.txt").read_text() == "Xhello\nworld\n" and (D / "note.txt").stat().st_mode & 0o777 == 0o600
    t.keys("Esc", "/mv", "Enter", "F6", "Enter")  # move
    t.until(lambda s: "✓ moved mv.txt" in s, "moved")
    assert (D / "mv.txt").read_text() == "move me" and not (B / "mv.txt").exists()
    t.keys("Esc", "F7", "newdir", "Enter")  # new folder, rename, delete
    assert (B / "newdir").is_dir()
    t.keys("R", "^U", "renamed", "Enter")
    assert (B / "renamed").is_dir() and not (B / "newdir").exists()
    t.keys("F8", "y")
    t.until(lambda s: "✓ deleted renamed" in s, "deleted")
    assert not (B / "renamed").exists()

    t.keys("+", "^U", "*.txt", "Enter")  # marks: by pattern, all / none, one by one, unmark by pattern
    t.until(lambda s: "[1]" in s and "*note.txt" in s, "marked by pattern")
    t.keys("*")  # marks there: none
    t.until(lambda s: "[" not in s.split("\n")[0].split("┬")[0], "no marks")
    t.keys("*", "-", "^U", "*.zip", "Enter")
    t.until(lambda s: "*note.txt" in s and "*data.zip" not in s, "unmarked by pattern")
    t.keys("Esc", ".")  # dotfiles, sort
    t.until(lambda s: ".dot" not in s.split("┬")[0] and "dotfiles hidden" in s, "dotfiles hidden")
    t.keys(".", "S")
    t.until(lambda s: ".dot" in s and "sorted by size" in s, "sorted by size")
    t.keys("S", "S")

    t.keys("/data", "Enter", "Enter")  # an archive: in, a folder in it, view, out at its top
    t.until(lambda s: "data.zip:/" in s and "inside/" in s, "inside the archive")
    t.keys("Down", "Enter", "Down", "F3")
    t.until(lambda s: "AAAA" in s, "a file in the archive")
    t.keys("q", "h", "h")
    t.until(lambda s: "data.zip" in s and "data.zip:/" not in s, "out of the archive")
    t.keys("Esc", "/m.mark", "Enter", "Enter")  # [open] runs your command
    t.until(lambda s: "F10 quit" in s and (B / "m.marker.opened").exists(), "[open]")
    t.keys("Esc", "/note", "Enter", "F2", "Enter", "Enter")  # F2 runs a [commands] entry, then waits for ⏎
    t.until(lambda s: (B / "menu.out").exists(), "F2 command")
    assert (B / "menu.out").read_text() == "hi note.txt\n"
    t.keys("^O")  # a shell here
    time.sleep(0.5)
    t.keys("echo here > shell.out; exit\n")
    t.until(lambda s: "F10 quit" in s and (B / "shell.out").exists(), "shell")

    t.keys("Esc", "~")  # home, go to, the host list, the config in rshm's own editor, theme, reload, help
    t.until(lambda s: f"local:{T} " in s, "home")
    t.keys("g", "^U", str(B), "Enter", "F9")
    t.until(lambda s: "─ hosts" in s.split("┬")[0] and "▸ here" in s, "host list")
    t.keys("Down", "Enter")  # a group unfolds, h folds it again
    t.until(lambda s: "▾ here" in s and "jumped" in s, "group unfolded")
    t.keys("h")
    t.until(lambda s: "▸ here" in s and "jumped" not in s, "group folded")
    t.keys("e")
    t.until(lambda s: "[hosts]" in s and "^S save" in s, "config in the editor")
    t.keys("^Q", "t")
    t.until(lambda s: "theme: matrix" in s, "theme")
    assert 'theme = "matrix"' in (T / "rshm.toml").read_text()
    t.keys("Esc", "r", "F1")
    t.until(lambda s: "help: all keys" in s and "copy to the other pane" in s, "help")
    t.keys("Esc")

    t.click(2, 5)  # mouse: a click selects, a second opens; .. is the first row
    t.click(1, 5)
    t.click(1, 5)
    t.until(lambda s: f"local:{T} " in s, "mouse: up a folder")

    t.resize(40, 60)  # upright: stacked; small: one pane, said once; back
    t.until(lambda s: s.split("\n")[0].startswith("┌─ local") and any(r.startswith("├─") for r in s.split("\n")), "stacked")
    t.resize(9, 30)
    t.until(lambda s: "one pane: Tab swaps" in s and "┬" not in s and "├" not in s, "one pane")
    t.keys("Tab")
    t.until(lambda s: f"local:{D}"[-10:] in s, "Tab shows the other pane")
    t.resize(4, 30)
    t.until(lambda s: "terminal too small" in s, "too small")
    t.resize(30, 120)
    t.until(lambda s: "┬" in s, "side by side again")
    assert t.quit() == 0

    # --- 2. hosts, through a throwaway sshd ---
    sshd = shutil.which("sshd") or next((p for p in ("/usr/sbin/sshd", "/usr/local/sbin/sshd") if os.path.exists(p)), None)
    sftp = next((p for p in ("/usr/lib/openssh/sftp-server", "/usr/libexec/openssh/sftp-server", "/usr/libexec/sftp-server",
                             "/usr/lib/ssh/sftp-server") if os.path.exists(p)), None)
    if not (sshd and sftp):
        sys.exit("screen ok (no sshd/sftp-server here: hosts skipped)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    for k in ("host", "user"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", T / k], check=True)
    (T / ".ssh").mkdir(mode=0o700, exist_ok=True)
    (T / "user.pub").rename(T / "authorized_keys")
    (T / "sshd_config").write_text(f"Port {port}\nListenAddress 127.0.0.1\nHostKey {T}/host\nAuthorizedKeysFile "
                                   f"{T}/authorized_keys\nPidFile {T}/sshd.pid\nStrictModes no\nUsePAM no\n"
                                   f"PasswordAuthentication no\nSubsystem sftp {sftp}\n")
    srv = subprocess.Popen([sshd, "-D", "-e", "-f", T / "sshd_config"], stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    me = os.environ.get("USER") or subprocess.run(["id", "-un"], stdout=subprocess.PIPE, text=True).stdout.strip()
    (T / ".ssh" / "config").write_text(f"Host direct jumped\n\tUser {me}\n\tPort {port}\n\tIdentityFile {T}/user\n"
                                       f"\tUserKnownHostsFile {T}/known_hosts\n\tStrictHostKeyChecking accept-new\n"
                                       "Host direct\n\tHostName 127.0.0.1\nHost jumped\n\tHostName localhost\n\tProxyJump direct\n")
    # ssh reads the real user's ~/.ssh/config, not $HOME's: point it at this one (jump children get -F too)
    (T / "bin").mkdir()
    (T / "bin" / "ssh").write_text(f'#!/bin/sh\nexec {shutil.which("ssh")} -F {T}/.ssh/config "$@"\n')
    (T / "bin" / "ssh").chmod(0o755)
    os.environ["PATH"] = f"{T}/bin:{os.environ['PATH']}"
    sockets = lambda: sorted(p.name for p in (T / ".ssh").glob("rshm-" + "[0-9a-f]" * 16))
    try:
        L = T / "up"
        (L / "sub").mkdir(parents=True)
        data = os.urandom(3 * 1024 * 1024 + 7)
        (L / "big.bin").write_bytes(data)
        (L / "sub" / "x.sh").write_text("#!/bin/sh\n")
        os.chmod(L / "sub" / "x.sh", 0o750)
        (T / "remote").mkdir()
        (T / "remote" / "r.txt").write_text("remote\n")
        t = Term(["jumped"], L)  # `rshm jumped`: the host on the right, this machine on the left
        t.until(lambda s: "jumped:/" in s and "big.bin" in s, "both panes")
        assert len(sockets()) == 1, sockets()
        t.keys("Tab", "/big", "Enter", "c", "^U", str(T / "remote"), "Enter")  # copy with the keys
        t.until(lambda s: "✓ → big.bin" in s, "copied")
        assert (T / "remote" / "big.bin").read_bytes() == data
        t.keys("Tab", "s")  # a shell on the host, on the same login; exit brings the screen back
        time.sleep(1)
        t.keys("echo RSHM_$((1+1)); exit\n")
        t.until(lambda s: b"RSHM_2" in b"".join(t.raw) and "F10 quit" in s and "RSHM_2" not in s, "shell, then the screen")
        assert len(sockets()) == 1, sockets()
        t.keys("p")  # the live check
        t.until(lambda s: "✓ jumped answers" in s, "live check")
        t.keys("g", "^U", str(T / "remote"), "Enter", "/r.txt", "Enter", "F3")  # a host's file, rshm's viewer and editor
        t.until(lambda s: s.startswith("remote") and "jumped:" in s, "remote file viewed")
        t.keys("q", "F4", "Z", "^S", "^Q")
        t.until(lambda s: "F10 quit" in s and (T / "remote" / "r.txt").read_text() == "Zremote\n", "remote file edited")
        assert t.quit() == 0
        time.sleep(2)
        assert sockets() == [], f"the screen left logins behind: {sockets()}"

        h = config.find(config.hosts(config.read(T / "rshm.toml")), "jumped")[0][1]
        R, Lo = fs.Remote(h, term=lambda cmd: 1, say=lambda m: None), fs.Local()
        tick = lambda *a, **k: None
        for src, sp, dst, dp in ((Lo, L, R, T / "remote" / "t"), (R, T / "remote" / "t", Lo, T / "down"),
                                 (R, T / "remote" / "t" / "big.bin", R, T / "remote" / "same.bin")):  # the last within one host
            tick.finished = set()
            fs.copy(src, str(sp), dst, str(dp), tick)
        fs.save(R, str(T / "remote" / "saved.txt"), b"over ssh\n")  # what the built-in editor's ^S does
        R.chattr(str(T / "remote" / "saved.txt"), {"perm": 0o604})
        R.close()
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        assert sha(T / "down" / "big.bin") == sha(L / "big.bin") == sha(T / "remote" / "t" / "big.bin") == sha(T / "remote" / "same.bin")
        assert (T / "remote" / "saved.txt").read_bytes() == b"over ssh\n" and (T / "remote" / "saved.txt").stat().st_mode & 0o777 == 0o604
        assert (T / "down" / "sub" / "x.sh").stat().st_mode & 0o777 == 0o750 & ~os.umask(os.umask(0))
        assert not list(T.rglob("*" + fs.PART)) and len(sockets()) == 1
        assert ssh.quiet([*ssh.argv(h, batch=True, timeout=3), "jumped", "true"])[0] == 0  # the live check
        ssh.logout()
        time.sleep(2)
        assert sockets() == [], f"logins left behind: {sockets()}"
    finally:
        srv.terminate()
        srv.wait()
    print("live ok")
finally:
    shutil.rmtree(T, ignore_errors=True)
