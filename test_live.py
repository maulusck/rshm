#!/usr/bin/env python3
"""rshm for real: the single file it builds of itself, on a real screen in a terminal (read back with pyte).
1. this machine: every key, each effect checked on screen or on disk; the layouts as the terminal turns and shrinks
2. hosts (when sshd is here): a throwaway sshd, reached through a jump set up in an ssh config as it should be;
   open, copy with the keys, a shell, a tree both ways, a copy within the host, the live check; nothing left behind
Runs as you, in a temp folder removed afterwards, sshd on a free local port. No root, no network, no other users."""
import contextlib, curses, fcntl, hashlib, os, pty, re, select, shutil, signal, socket, struct, subprocess, sys, tempfile
import termios, time
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


TERM = os.environ.get("RSHM_TERM", "xterm")  # the keys as this terminal sends them, from its terminfo
curses.setupterm(TERM, sys.__stdout__.fileno())
cap = lambda c: (curses.tigetstr(c) or b"").decode()
KEY = {**{f"F{i}": cap(f"kf{i}") for i in range(1, 11)}, "Up": cap("kcuu1"), "Down": cap("kcud1"), "Right": cap("kcuf1"),
       "Left": cap("kcub1"), "Esc": "\x1b", "Tab": "\t", "Enter": "\r", **{f"^{c}": chr(ord(c) - 64) for c in "FKOSUX"}}
COLOURS = curses.tigetnum("colors")  # 256, 8, or none (-1, -2)
MOUSE = cap("kmous")  # "\x1b[<": SGR; "\x1b[M": X10 bytes; none: the mouse steps are skipped
SIZES = [(1, 1), (2, 10), (3, 3), (6, 19), (7, 20), (8, 24), (24, 80), (12, 40), (40, 60), (4, 200), (60, 30), (24, 81)]


class Term:  # one rshm in a pseudo terminal, its screen kept by pyte
    def __init__(t, args, cwd, rows=30, cols=120, env=(), cmd=None):  # env: what this terminal is (TERM, LANG,
        # NO_COLOR); cmd: what runs on it instead of rshm (a shell)
        t.screen, t.raw = Screen(cols, rows), []
        t.stream = Stream(t.screen)
        t.pid, t.fd = pty.fork()
        if not t.pid:
            os.chdir(cwd)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            os.environ["TERM"], os.environ["GPG_TTY"], os.environ["LANG"] = TERM, os.ttyname(0), "C.UTF-8"
            for k in ("PYTHONPATH", "LC_ALL", "LC_CTYPE", "NO_COLOR"):
                os.environ.pop(k, None)
            os.environ.update(env)
            run = os.environ.get("RSHM_RUN", sys.executable).split()  # RSHM_RUN: a python to run it with (a tracer)
            cmd = cmd or [*run, str(T / "rshm.pyz"), *args]  # finds rshm.toml beside it
            os.execvp(cmd[0], cmd)

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

    def click(t, y, x, b=0):  # press and release at a cell (b 2: the right button), as this terminal reports it
        os.write(t.fd, (f"\x1b[<{b};{x + 1};{y + 1}M\x1b[<{b};{x + 1};{y + 1}m" if MOUSE == "\x1b[<" else
                        f"\x1b[M{chr(32 + b)}{chr(33 + x)}{chr(33 + y)}\x1b[M#{chr(33 + x)}{chr(33 + y)}").encode())
        t.pump()

    def sweep(t, back, what):  # this screen through every size, tiny to wide, and back: alive, no error, as it was
        for rows, cols in SIZES:
            t.resize(rows, cols)
        t.resize(30, 120)
        assert os.waitpid(t.pid, os.WNOHANG) == (0, 0), f"{what}: rshm died resizing"
        t.until(lambda s: back(s) and "ERR" not in s and "Error" not in s, f"{what}, after every size")

    def quit(t):
        os.write(t.fd, b"qq")  # q asks, q again says yes
        with contextlib.suppress(OSError):  # read on to the end: a full terminal would hold rshm's exit up
            while os.read(t.fd, 65536):
                pass
        return os.waitpid(t.pid, 0)[1]


try:
    # the single file, built by rshm itself, with its config beside it, as you'd carry it on a USB stick
    subprocess.run([sys.executable, "-m", "rshm", "--pyz", T / "rshm.pyz"], cwd=Path(__file__).resolve().parent, check=True)
    for old in filter(None, map(shutil.which, ("python3.8", "python3.7"))):  # an older python says what it needs
        assert subprocess.run([old, T / "rshm.pyz"], capture_output=True, text=True).stderr == "rshm needs Python 3.9 or newer\n"
    (T / "rshm.toml").write_text('theme = "default"\n[open]\n"*.marker" = "touch {file}.opened"\n'
                                 f'"*.lnk" = "cp {{file}} {T}/got.txt"\n'
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
    (B / "sub" / "deep.txt").write_text("deep")
    with __import__("zipfile").ZipFile(B / "data.zip", "w") as z:
        z.writestr("inside/a.txt", "A" * 300)
    t = Term([], B)
    rows = lambda s: s.split("\n")
    left = lambda s: "\n".join(r.split("│")[0] for r in rows(s)[2:])  # pane 1's rows, side by side
    if not config.tomllib:  # Python 3.9, 3.10: the config is left unread, said once; this machine works as ever
        t.until(lambda s: "rshm.toml: ignored, needs Python 3.11" in s and "note.txt" in s, "config unread, said")
        t.keys("Tab", "Enter", "g", "^U", str(D), "Enter", "Tab", "/note", "Enter", "c", "Enter")
        t.until(lambda s: re.search(r"✓ copied note\.txt to local:\S+  \S+ in \d", s), "a copy")
        t.keys("F3")  # (the filter still holds the cursor on note.txt)
        t.until(lambda s: "hello" in s and "/ ? find" in s, "the viewer")
        t.keys("q")
        assert t.quit() == 0 and (D / "note.txt").read_text() == "hello\nworld\n"
    else:  # the config's own: [open], [commands], the host group, themes
        t.until(lambda s: " 1  local:" in s and "note.txt" in s and "2  hosts" in s and "9pulldown" in s, "menus, pane 1 here, pane 2  hosts")
        t.keys("Tab", "Enter", "g", "^U", str(D), "Enter", "Tab")  # pane 2: this machine too, in dst
        t.until(lambda s: f"2  local:{D}" in s, "pane 2 in dst")

        t.keys("/note", "Enter", "F3")  # view: less's keys; a search lights its match; hex; back
        t.until(lambda s: "hello" in s and "/ ? find" in s, "viewer")
        t.keys("/wor", "Enter")
        lit = lambda y: t.screen.buffer[y][0].bg != "default" or t.screen.buffer[y][0].reverse
        t.until(lambda s: lit(1) and not lit(0), "viewer search: the match lit")
        t.keys("x")
        t.until(lambda s: "68 65 6c 6c 6f" in s, "hex")
        t.keys("q", "F4", "^K", "^U", "X", "^S", "^X")  # edit, nano's keys: cut a line, paste it back (cursor after it), type
        t.until(lambda s: "10quit" in s and (B / "note.txt").read_text() == "hello\nXworld\n", "edited")
        t.keys("i")  # info
        t.until(lambda s: "name  note.txt" in s and "-rw-r--r--" in s, "info")
        t.keys("q", "A", "^U", "600", "Enter")  # attributes: only what's typed changes (here the mode)
        t.until(lambda s: "-rw------- " in rows(s)[-5] or "-rw------- " in s.split("└─")[-1], "the ls -l line")
        assert (B / "note.txt").stat().st_mode & 0o777 == 0o600
        t.keys("c", "Enter")  # F5 copy to other pane: mode and time travel
        t.until(lambda s: "✓ copied note.txt to " in s, "copied")
        assert (D / "note.txt").read_text() == "hello\nXworld\n" and (D / "note.txt").stat().st_mode & 0o777 == 0o600 and (D / "note.txt").stat().st_mtime == int((B / "note.txt").stat().st_mtime)
        t.keys("Esc", "/mv", "Enter", "F6", "Enter")  # move
        t.until(lambda s: "✓ moved mv.txt to " in s, "moved")
        assert (D / "mv.txt").read_text() == "move me" and not (B / "mv.txt").exists()
        t.keys("Esc", "F7", "newdir", "Enter", "R", "^U", "renamed", "Enter")  # new folder, rename, delete
        assert (B / "renamed").is_dir() and not (B / "newdir").exists()
        t.keys("F8", "y")
        t.until(lambda s: "✓ deleted renamed/ in local:" in s and not (B / "renamed").exists(), "deleted")

        t.keys("/note", "Enter", "L", "^U", str(B / "ln.txt"), "Enter")  # a symlink: one question, where; then retarget it
        t.until(lambda s: (B / "ln.txt").is_symlink(), "symlink")
        assert os.readlink(B / "ln.txt") == "note.txt"  # relative to where it is
        t.keys("Esc", "/ln.t", "Enter")
        t.until(lambda s: "ln.txt -> note.txt" in s.split("└─")[1] and "ln.txt@ -> note.txt" in left(s), "target: row and ls -l line")
        t.keys("i")
        t.until(lambda s: "points to  note.txt" in s, "target: info")
        t.keys("q")
        t.keys("L", "^U", "data.zip", "Enter")
        t.until(lambda s: os.readlink(B / "ln.txt") == "data.zip", "retargeted")
        t.keys("Esc", "/note", "Enter", "H", "^U", str(B / "hard.txt"), "Enter")
        t.until(lambda s: (B / "hard.txt").exists() and (B / "hard.txt").stat().st_ino == (B / "note.txt").stat().st_ino, "hard link")
        t.keys("i")  # a hard link: nothing marks it apart (it is the same file); info says how many names it has
        t.until(lambda s: "links  2: the same file under 2 names (hard links)" in s, "info: hard links")
        t.keys("q")
        t.keys("Esc")

        (B / "mv" ).mkdir()  # the guards: into itself, two of one name, a folder merge on move, a tree with a pipe, a pipe
        (B / "mv" / "a.txt").write_text("a")
        (D / "mv").mkdir()
        (D / "mv" / "b.txt").write_text("b")
        (B / "fd").mkdir()
        (B / "fd" / "f.txt").write_text("f")
        os.mkfifo(B / "fd" / "p")
        (D / "fd").mkdir()  # there already: the move merges, so it copies, and a pipe can't be copied
        os.mkfifo(B / "pipe")
        t.keys("r", "/sub", "Enter", "c", "^U", str(B / "sub"), "Enter")  # r: what was just made shows
        t.until(lambda s: "cannot copy a directory into itself" in s, "copy into itself refused")
        t.keys("Esc", "/mv", "Enter", "F6", "Enter", "y")
        t.until(lambda s: "✓" in s.split("└─")[-1] and not (B / "mv").exists(), "a folder merged on move")
        assert sorted(x.name for x in (D / "mv").iterdir()) == ["a.txt", "b.txt"]
        t.keys("Esc", "/fd", "Enter", "F6", "Enter", "y")
        t.until(lambda s: "fd kept" in s, "a merged tree with a pipe stays")
        assert (D / "fd" / "f.txt").read_text() == "f" and (B / "fd" / "p").exists()
        t.keys("Esc", "/pipe", "Enter", "Enter")
        t.until(lambda s: "pipe: not a regular file" in s, "a pipe isn't opened")
        t.keys("Esc", "g", "^U", "sub", "Enter")  # a relative path: from the pane's folder
        t.until(lambda s: f"1  local:{B}/sub" in s, "go to sub")
        (B / "sub" / "note.txt").write_text("dup")
        t.keys("~", "g", "^U", str(B), "Enter", "^F", "^U", "note.txt", "Enter", "*", "c", "Enter")
        t.until(lambda s: "duplicate names in selection" in s, "two of one name refused")
        (B / "sub" / "note.txt").unlink()
        t.keys("h", "Esc", "/note", "Enter", "F4", "\x1c", "hello", "Enter", "Esc", "^X")  # replace, then Esc: nothing
        t.until(lambda s: "10quit" in s, "back from the editor")
        assert (B / "note.txt").read_text() == "hello\nXworld\n"
        t.keys("Esc")

        t.keys("+", "^U", "*.txt", "Enter")  # marks: by pattern, none, all, unmark by pattern; what differs over there
        t.until(lambda s: "[3]" in rows(s)[1] and "*note.txt" in s, "marked by pattern")
        t.keys("*")
        t.until(lambda s: "[" not in rows(s)[1].split("┬")[0], "no marks")
        t.keys("*", "-", "^U", "*.zip", "Enter")
        t.until(lambda s: "*note.txt" in s and "*data.zip" not in s, "unmarked by pattern")
        t.keys("Esc", "D")
        t.until(lambda s: "differ, marked" in s and "*data.zip" in s and "*note.txt" not in s, "compare")
        t.keys("Esc", ".")  # dotfiles, sort menu
        t.until(lambda s: ".dot" not in left(s) and "dotfiles hidden" in s, "dotfiles hidden")
        t.keys(".", "S", "Down", "Down", "Enter")
        t.until(lambda s: ".dot" in left(s) and "sorted by size" in s, "sorted by size")
        t.keys("S", "Down", "Down", "Down", "Down", "Enter")
        t.until(lambda s: "sorted by size, reversed" in s, "reversed")
        t.keys("S", "Down", "Down", "Down", "Down", "Enter", "S", "Enter")
        t.until(lambda s: s.count("sorted by name") and "sorted by name, reversed" not in s.split("\n")[-2], "back to by name")
        t.keys("/sub", "Enter", "z")  # size of a folder
        t.until(lambda s: "sub: 4B in 1 file" in s, "size")

        (B / "sub" / "gone.txt").write_text("x")  # find: results as the listing; links in them, a delete, a link made
        (B / "sub" / "deep.lnk").symlink_to("deep.txt")
        t.keys("Esc", "^F", "^U", "*", "Enter")
        t.until(lambda s: "sub/deep.lnk@ -> deep.txt" in s and "sub/gone.txt" in s, "find: every entry, links with targets")
        t.keys("/gone", "Enter", "F8", "y")
        t.until(lambda s: "sub/gone.txt" not in left(s).split("└─")[0] and "1  find * in local:" in s, "a delete leaves the find results")
        t.keys("Esc", "/deep.t", "Enter", "H", "Enter")
        t.until(lambda s: (D / "deep.txt").exists(), "a link from a find result: by its own name, in the other pane")
        assert (D / "deep.txt").stat().st_ino == (B / "sub" / "deep.txt").stat().st_ino
        t.keys("Esc", "^F", "^U", "deep*", "Enter")  # ⏎ on a result goes there; history back and forth
        t.until(lambda s: "1  find deep* in local" in s and "sub/deep.txt" in s, "find")
        t.keys("Down", "Enter")
        t.until(lambda s: f"1  local:{B}/sub" in s, "to the found file's folder")
        t.keys("b")
        t.until(lambda s: f"1  local:{B} " in s, "back")
        t.keys("f")
        t.until(lambda s: f"1  local:{B}/sub" in s, "forward")
        t.keys("b", "o")  # the other pane here; swap; swap back
        t.until(lambda s: f"2  local:{B} " in s, "other pane here")
        t.keys("^U")
        t.until(lambda s: rows(s)[1].startswith("┌─ 1  local") and rows(s)[1].count(f"local:{B} ") == 2, "swapped")
        t.keys("^U", "Tab", "b", "Tab")
        t.until(lambda s: f"2  local:{D}" in s, "pane 2 back in dst")

        t.keys("/data", "Enter", "Enter")  # an archive: in, a folder in it, view, out at its top
        t.until(lambda s: "data.zip:/" in s and "inside/" in s, "inside the archive")
        t.keys("Down", "Enter", "Down", "F3")
        t.until(lambda s: "AAAA" in s, "a file in the archive")
        t.keys("q", "h", " ")  # a mark inside means nothing outside
        t.until(lambda s: "[1]" in rows(s)[1], "marked inside")
        t.keys("h")  # at the archive's top: out of it
        t.until(lambda s: "data.zip" in s and "data.zip:/" not in s and "[" not in rows(s)[1].split("┬")[0], "out, unmarked")
        t.keys("Esc", "/m.mark", "Enter", "Enter")  # [open] runs your command
        t.until(lambda s: "10quit" in s and (B / "m.marker.opened").exists(), "[open]")
        t.keys("Esc", "/note", "Enter", "F2", "Up", "Enter", "Enter")  # F2: the menu; ↑ wraps to your [commands], last; a wait for ⏎
        t.until(lambda s: (B / "menu.out").exists(), "F2 command")
        assert (B / "menu.out").read_text() == "hi note.txt\n"
        t.keys("^O")  # a shell here
        time.sleep(0.5)
        t.keys("echo here > shell.out; exit\n")
        t.until(lambda s: "10quit" in s and (B / "shell.out").exists(), "shell")

        t.keys("Esc", "~")  # home, go to; the host list (a group folds), the config in rshm's own editor; theme; help
        t.until(lambda s: f"1  local:{T} " in s, "home")
        t.keys("g", "^U", str(B), "Enter", "@")
        t.until(lambda s: "1  hosts" in s and "▸ here" in s, "host list")
        t.keys("Down", "Enter")
        t.until(lambda s: "▾ here" in s and "jumped" in s, "group unfolded")
        t.keys("h")
        t.until(lambda s: "▸ here" in s and "jumped" not in s, "group folded")
        t.keys("F4")
        t.until(lambda s: "[hosts]" in s and "^S save" in s, "config in the editor")
        t.keys("^X", "Esc", "t")  # the themes this terminal can show: 256 colours all, 8 the default one, none none
        if COLOURS >= 8:
            t.until(lambda s: "─ theme " in s and re.search(r"default +✓", s) and ("matrix" in s) == (COLOURS >= 256), "theme picker")
            t.keys("Enter")
            t.until(lambda s: "theme: default" in s, "theme")
        else:
            t.until(lambda s: "no themes: terminal has no colours" in s, "no colours, no themes")
        t.keys("F1")
        t.until(lambda s: "copy to other pane" in s and "F5 c" in s, "help: every key")
        t.keys("Esc", "F9")  # the menu bar: F9, ← → between menus (one open, lit in the bar), Esc; taps on menus and items
        t.until(lambda s: "host list" in s and "swap panes" in s, "menu: Pane 1")
        t.keys("Right", "Right")
        cell = lambda word: t.screen.buffer[0][rows(t.text())[0].index(word)]
        lit = lambda word: (cell(word).bg, cell(word).reverse) != (cell("Pane 2").bg, cell("Pane 2").reverse)
        t.until(lambda s: "mark by pattern" in s and "information" not in s and "swap panes" not in s and lit("Mark")
                and not lit("File"), "menu: Mark, and only Mark")
        t.keys("Esc")
        t.until(lambda s: "mark by pattern" not in s and not lit("Mark"), "menus closed")
        if MOUSE:  # taps: bar, menu item, F-key bar, titles, rows, right-click (no mouse in this terminal: keys only)
            t.click(0, rows(t.text())[0].index("Options") + 1)
            t.until(lambda s: "edit config" in s and "theme" in s, "Options by a tap")
            y = next(i for i, r in enumerate(rows(t.text())) if re.search(r"theme +t ", r))
            t.click(y, rows(t.text())[y].index("theme") + 1)
            t.until(lambda s: "─ theme " in s, "an item by a tap: the theme picker")
            t.keys("Esc")
            t.click(len(rows(t.text())) - 1, rows(t.text())[-1].index("7mkdir") + 1)  # the F-key bar answers taps
            t.until(lambda s: "mkdir:" in s, "F7 by a tap")
            t.keys("Esc")
            t.click(1, 5)  # a tap on a title: the host list; again: back
            t.until(lambda s: "1  hosts" in s, "title tap: host list")
            t.click(1, 5)
            t.until(lambda s: f"1  local:{B}" in s, "title tap: back")
            t.click(3, 5)  # a tap selects, a second opens; right-click: the menu of what's under it
            t.click(2, 5)
            t.click(2, 5)
            t.until(lambda s: f"1  local:{T} " in s, "mouse: up a folder")
            t.click(4, 5, 2)
            t.until(lambda s: "information" in s and "symbolic link" in s, "right-click menu")
            t.keys("Esc")
        for k in ("[", "[", "]"):  # the log: older, newer
            t.keys(k)
        t.until(lambda s: "↑1" in s, "log scrolled")
        t.keys("]")
        t.until(lambda s: "↑" not in rows(s)[-5], "log at the newest")

        t.resize(40, 60)  # upright: stacked; small: one pane, said once; back
        t.until(lambda s: rows(s)[1].startswith("┌─ 1  local") and any(r.startswith("├─ 2") for r in rows(s)), "stacked")
        t.resize(9, 30)
        t.until(lambda s: "one pane: Tab swaps" in s and "┬" not in s and "├" not in s, "one pane")
        t.keys("Tab")
        t.until(lambda s: " 2 " in rows(s)[1], "Tab shows pane 2")
        t.resize(5, 30)
        t.until(lambda s: "too small: 30×5, needs 20×7" in s, "too small")
        t.resize(30, 120)
        t.until(lambda s: "┬" in s, "side by side again")
        t.sweep(lambda s: "10quit" in s, "the panes")  # every screen through every size, a tiny terminal included
        t.keys("g", "^U", str(B), "Enter", "/note", "Enter", "F3")
        t.sweep(lambda s: "/ ? find" in s and "hello" in s, "the viewer")
        t.keys("q", "F4", "Y")
        t.sweep(lambda s: "^S save" in s and rows(s)[0].startswith("Yhello"), "the editor, an edit unsaved")
        t.keys("^S", "^X")
        t.until(lambda s: "10quit" in s and (B / "note.txt").read_text() == "Yhello\nXworld\n", "the edit kept through it all")
        t.keys("F9")
        t.sweep(lambda s: "host list" in s and "swap panes" in s, "a menu")
        t.keys("Esc", "g")
        t.sweep(lambda s: "go to:" in s, "a prompt")
        t.keys("Esc", "F1")
        t.sweep(lambda s: "copy to other pane" in s, "help")
        t.keys("Esc")
        assert t.quit() == 0

    t = Term([], B)  # ^C: asks, as q does; a key, never a signal that could eat the next one. ^C again: ended as ^C ends
    t.until(lambda s: "10quit" in s, "started")
    t.keys("\x03")
    t.until(lambda s: "quit? (y/N)" in s, "^C asks")
    t.keys("n", "\x03")
    t.until(lambda s: "quit? (y/N)" in s, "^C asks again")
    os.write(t.fd, b"\x03")
    with contextlib.suppress(OSError):
        while os.read(t.fd, 65536):
            pass
    assert os.waitstatus_to_exitcode(os.waitpid(t.pid, 0)[1]) == 130

    sh = Term([], B, env={"PS1": "$ "}, cmd=["bash", "--norc", "--noprofile", "-i"])  # ^Z from a shell with job
    run = " ".join(os.environ.get("RSHM_RUN", sys.executable).split() + [str(T / "rshm.pyz")])
    sh.keys(f"{run}\r")  # control: stopped, the shell back; fg: rshm as it was
    sh.until(lambda s: "10quit" in s and "note.txt" in s, "rshm from a shell")
    sh.keys("\x1a")
    sh.until(lambda s: "Stopped" in s, "^Z: stopped, the shell back")
    sh.keys("fg\r")
    sh.until(lambda s: "10quit" in s and "Stopped" not in s.split("\n")[-3:], "fg: back")
    sh.keys("\x0c", "\x03", "\x03", "echo rc=$?\r")
    sh.until(lambda s: "rc=130" in s, "^C ^C after a suspend: ended as ^C ends")
    os.write(sh.fd, b"exit\r")
    with contextlib.suppress(OSError):  # (read to its end: the terminal closes with the shell)
        while os.read(sh.fd, 65536):
            pass
    assert os.waitpid(sh.pid, 0)[1] == 0

    # --- terminals: each draws what it can, nothing it can't ---
    t = Term([], B, env={"TERM": "vt100", "LANG": "C"})  # no UTF-8, no colours, can't hide its cursor
    t.until(lambda s: rows(s)[1].startswith("+- 1  local:") and "10quit" in s, "vt100, ASCII")
    t.keys("\x1bOQ")  # vt100's F2
    t.until(lambda s: "| open" in s and "+---" in s, "a menu, framed in ASCII")
    sgr = lambda raw: re.findall(rb"\x1b\[([0-9;]*)m", raw)  # the text attributes sent; colours: 30-48, 90-107
    colour = lambda a: re.search(rb"(^|;)(3[0-8]|4[0-8]|9[0-7]|10[0-7])(;|$)", a)  # (39, 49: the terminal's own)
    assert all(b < 128 for b in b"".join(t.raw)) and not any(colour(a) for a in sgr(b"".join(t.raw)))
    t.keys("Esc")
    assert t.quit() == 0
    t = Term([], B, env={"TERM": "xterm-256color"})  # 256 colours: every theme; the pick saved
    t.keys("t")
    t.until(lambda s: "─ theme " in s and "matrix" in s and "ember" in s, "every theme, 256 colours")
    t.keys("j", "\r")
    t.until(lambda s: "theme: matrix" in s, "a 256-colour theme")
    assert 'theme = "matrix"' in (T / "rshm.toml").read_text() and t.quit() == 0
    if config.tomllib:  # read back: the config, Python 3.11 on
        t = Term([], B, env={"TERM": "xterm"})  # back on 8 colours: the default theme shown, said once, the choice kept
        t.until(lambda s: "theme matrix: terminal has 8 colours, using default" in s, "a theme this terminal can't show")
        assert 'theme = "matrix"' in (T / "rshm.toml").read_text() and t.quit() == 0
    t = Term([], B, env={"TERM": "xterm-kitty"})  # no terminfo for it here: drawn as xterm-256color, said in the log
    t.until(lambda s: "TERM=xterm-kitty unknown, using xterm-256color" in s and "10quit" in s, "TERM unknown")
    assert t.quit() == 0
    t = Term([], B, env={"TERM": "xterm-256color", "NO_COLOR": "1"})  # no colours, bold and reverse kept
    t.until(lambda s: "NO_COLOR set" in s and "10quit" in s, "NO_COLOR")
    assert not any(colour(a) for a in sgr(b"".join(t.raw))) and any(sgr(b"".join(t.raw)))
    assert t.quit() == 0
    t = Term([], B, env={"TERM": "dumb"})  # can't place its cursor: refused, plainly, nothing drawn
    out = b""
    with contextlib.suppress(OSError):  # (read to its end: the terminal closes when rshm leaves)
        while b := os.read(t.fd, 65536):
            out += b
    assert out.strip() == b"rshm: TERM=dumb cannot address the cursor", out
    assert os.waitpid(t.pid, 0)[1] != 0
    r = subprocess.run([*os.environ.get("RSHM_RUN", sys.executable).split(), T / "rshm.pyz"], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
    assert r.returncode == 1 and r.stderr == "rshm: not a terminal\n", r  # not a terminal at all

    if config.tomllib:  # the hosts here are the config's group (Python 3.11 on); stress covers SFTP on any
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
        (T / "bin" / "ssh").write_text(  # a login also gets a gpg-agent-like prompt, written straight onto the terminal
            f'#!/bin/sh\ncase " $* " in *" -N "*) printf "\\033[1;20HPINENTRY-WAS-HERE" > "$GPG_TTY";; esac\n'
            f'exec {shutil.which("ssh")} -F {T}/.ssh/config "$@"\n')
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
            (T / "remote" / "r.lnk").symlink_to("r.txt")
            t = Term(["jumped"], L)  # `rshm jumped`: the host on the right, this machine on the left
            t.until(lambda s: "jumped:/" in s and "big.bin" in s and "PINENTRY" not in s, "both panes, nothing left over")
            assert b"PINENTRY-WAS-HERE" in b"".join(t.raw)  # it was written, and drawn over
            assert len(sockets()) == 1, sockets()
            t.keys("Tab", "/big", "Enter", "c", "^U", str(T / "remote"), "Enter")  # copy with the keys
            t.until(lambda s: "✓ copied big.bin to jumped:" in s, "copied")
            assert (T / "remote" / "big.bin").read_bytes() == data
            t.keys("Tab", "s")  # a shell on the host, on the same login; exit brings the screen back
            time.sleep(1)
            t.keys("echo RSHM_$((1+1)); exit\n")
            t.until(lambda s: b"RSHM_2" in b"".join(t.raw) and "10quit" in s and "RSHM_2" not in s, "shell, then the screen")
            assert len(sockets()) == 1, sockets()
            t.keys("p")  # the live check
            t.until(lambda s: "✓ jumped: up" in s, "live check")
            t.keys("r", "@")  # a reload keeps the pane's host and its folder
            t.until(lambda s: "● jumped" in s, "reload: the host is still this pane's")
            t.keys("@")
            t.keys("g", "^U", str(T / "remote"), "Enter", "/r.txt", "Enter", "F3")  # a host's file, rshm's viewer and editor
            t.until(lambda s: s.startswith("remote") and "jumped:" in s, "remote file viewed")
            t.keys("q", "F4", "Z", "^S", "^X")
            t.until(lambda s: "10quit" in s and (T / "remote" / "r.txt").read_text() == "Zremote\n", "remote file edited")
            t.keys("Esc", "/r.lnk", "Enter")  # a host's link: its target shown; your [open] tool gets what it points to
            t.until(lambda s: "r.lnk@ -> r.txt" in s, "a host's link and its target")
            t.keys("Enter")
            t.until(lambda s: "10quit" in s and (T / "got.txt").exists() and (T / "got.txt").read_text() == "Zremote\n",
                    "a tool on a host's link gets its content")
            assert t.quit() == 0
            time.sleep(2)
            assert sockets() == [], f"the screen left logins behind: {sockets()}"

            h = config.find(config.hosts(config.read(T / "rshm.toml")), "jumped")[0][1]
            R, Lo = fs.Remote(h, tty=lambda fn, *a: fn(*a), say=lambda m: None), fs.Local()
            tick = lambda *a, **k: None
            for src, sp, dst, dp in ((Lo, L, R, T / "remote" / "t"), (R, T / "remote" / "t", Lo, T / "down"),
                                     (R, T / "remote" / "t" / "big.bin", R, T / "remote" / "same.bin")):  # the last within one host
                tick.finished, tick.skipped = set(), []
                fs.copy(src, str(sp), dst, str(dp), tick)
            fs.save(R, str(T / "remote" / "saved.txt"), b"over ssh\n")  # what the built-in editor's ^S does
            R.chattr(str(T / "remote" / "saved.txt"), {"perm": 0o604})
            R.close()
            sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            assert sha(T / "down" / "big.bin") == sha(L / "big.bin") == sha(T / "remote" / "t" / "big.bin") == sha(T / "remote" / "same.bin")
            assert (T / "remote" / "saved.txt").read_bytes() == b"over ssh\n" and (T / "remote" / "saved.txt").stat().st_mode & 0o777 == 0o604
            assert (T / "down" / "sub" / "x.sh").stat().st_mode & 0o777 == 0o750
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
