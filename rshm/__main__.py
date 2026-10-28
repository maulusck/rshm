"""rshm [HOST [HOST]]: one host opens in the right pane (this machine on the left), two side by side."""
import argparse, atexit, curses, importlib.resources, os, signal, sys, zipfile
from pathlib import Path

from . import __version__, config
from .ssh import logout
from .ui import App, keys_help


def pyz(out):  # this very rshm as one runnable file, from wherever it runs (source, pip, a .pyz)
    tmp = out.with_name(f".{out.name}.rshm-tmp")
    with open(tmp, "wb") as f:
        f.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
            for r in sorted(importlib.resources.files(__package__).iterdir(), key=lambda r: r.name):
                if r.name.endswith(".py"):
                    z.writestr(f"rshm/{r.name}", r.read_bytes())
            z.writestr("__main__.py", "import sys\nif sys.version_info < (3, 9):\n    sys.exit('rshm needs Python 3.9 or "
                                      "newer')\nfrom rshm.__main__ import main\nmain()\n")
    tmp.chmod(0o755)
    os.replace(tmp, out)


def terminal():  # the terminal ncurses will draw on -> a note for the log. A TERM this machine has no entry for
    term = os.environ.get("TERM", "")  # (kitty, ghostty over ssh) is drawn as xterm-256color, which every modern one
    if not (sys.stdin.isatty() and sys.stdout.isatty()):  # speaks; one that can't place its cursor (dumb): refused
        sys.exit("rshm needs a terminal")
    note = ""
    try:
        curses.setupterm()
    except curses.error:
        os.environ["TERM"] = "xterm-256color"
        try:
            curses.setupterm()
        except curses.error:
            sys.exit(f"rshm: no terminfo for TERM={term or '(unset)'} here, nor for xterm-256color")
        note = f"TERM={term or '(unset)'} is unknown here: drawn as xterm-256color"
    if not curses.tigetstr("cup"):
        sys.exit(f"rshm needs a terminal that can move its cursor; TERM={term} can't")
    return note


def main():
    ap = argparse.ArgumentParser(prog="rshm", description="Remote SHell Manager: a two-pane file manager for this "
                                 "machine and every host in your ~/.ssh/config, over plain ssh.",
                                 epilog=keys_help(), formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="*", help="up to two hosts to open (name, group/name or unique substring)")
    ap.add_argument("-c", "--config", type=Path, help=f"config file (default: {config.locate()}, if there)")
    ap.add_argument("-V", "--version", action="version", version=f"rshm {__version__}")
    ap.add_argument("--pyz", type=Path, metavar="FILE", help="write this rshm as one runnable file (python3 FILE) and exit")
    a = ap.parse_args()
    if a.pyz:
        return pyz(a.pyz.expanduser())
    cfg = a.config.expanduser() if a.config else config.locate()
    if a.config and not cfg.exists():
        ap.error(f"{cfg}: no such file")
    if a.config and config.unread(cfg):
        ap.error(config.unread(cfg))
    if len(a.host) > 2:
        ap.error("two panes: at most two hosts")
    start = []
    if a.host:  # resolved before the screen opens, so a typo is a plain error
        try:
            hs = config.hosts(config.read(cfg))
        except OSError as e:
            ap.error(f"{cfg}: {e}")
        for name in a.host:
            m = config.find(hs, name)
            if len(m) != 1:
                ap.error(f"ambiguous host {name!r}: {', '.join('/'.join(p) for p, _ in m)}" if m
                         else f"no host {name!r} in your config or ~/.ssh/config")
            start.append(m[0][0])
    for sig in (signal.SIGTERM, signal.SIGHUP):  # kill or a closed terminal unwind normally: terminal and logins cleaned
        signal.signal(sig, lambda n, _: sys.exit(128 + n))
    note = terminal()
    atexit.register(logout)
    try:
        curses.wrapper(lambda scr: App(scr, cfg, note).loop(start))
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        sys.exit(f"rshm needs a terminal ({e})")


if __name__ == "__main__":
    main()
