"""rshm [HOST [HOST]]: one host opens in the right pane (this machine on the left), two side by side."""
import argparse, atexit, curses, signal, sys, tomllib
from pathlib import Path

from . import __version__, config
from .ssh import logout
from .ui import App, keys_help


def main():
    ap = argparse.ArgumentParser(prog="rshm", description="Remote SHell Manager: a two-pane file manager for this "
                                 "machine and every host in your ~/.ssh/config, over plain ssh.",
                                 epilog=keys_help(), formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", nargs="*", help="up to two hosts to open (name, group/name or unique substring)")
    ap.add_argument("-c", "--config", type=Path, help=f"config file (default: {config.locate()}, if there)")
    ap.add_argument("-V", "--version", action="version", version=f"rshm {__version__}")
    a = ap.parse_args()
    cfg = a.config.expanduser() if a.config else config.locate()
    if a.config and not cfg.exists():
        ap.error(f"{cfg}: no such file")
    if len(a.host) > 2:
        ap.error("two panes: at most two hosts")
    start = []
    if a.host:  # resolved before the screen opens, so a typo is a plain error
        try:
            hs = config.hosts(config.read(cfg))
        except (OSError, tomllib.TOMLDecodeError) as e:
            ap.error(f"{cfg}: {e}")
        for name in a.host:
            m = config.find(hs, name)
            if len(m) != 1:
                ap.error(f"ambiguous host {name!r}: {', '.join('/'.join(p) for p, _ in m)}" if m
                         else f"no host {name!r} in your config or ~/.ssh/config")
            start.append(m[0][0])
    for sig in (signal.SIGTERM, signal.SIGHUP):  # kill or a closed terminal unwind normally: terminal and logins cleaned
        signal.signal(sig, lambda n, _: sys.exit(128 + n))
    atexit.register(logout)
    try:
        curses.wrapper(lambda scr: App(scr, cfg).loop(start))
    except KeyboardInterrupt:
        pass
    except curses.error as e:
        sys.exit(f"rshm needs a terminal ({e})")


if __name__ == "__main__":
    main()
