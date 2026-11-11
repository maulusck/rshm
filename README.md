# rshm

Two-pane file manager for this machine and any ssh host, over sftp. Midnight Commander's keys and layout. One file,
Python 3.9+ and OpenSSH, nothing else; a config is optional. Viewer, editor and archives built in.

```sh
python3 rshm.pyz              # this host, and the host list
python3 rshm.pyz web          # a host from the list in pane 2: name, group/name or a unique part of it
python3 rshm.pyz web db       # one host per pane
python3 rshm.pyz web:/srv     # ...in that directory; any ssh destination too: me@box, ssh://me@box:2222
python3 rshm.pyz /srv ~/x     # a directory alone is this machine's
python3 -m rshm --pyz FILE    # rshm as one executable file
```

Hosts: rshm's `[hosts]` and every `~/.ssh/config` alias, or any ssh destination. `g` on the host list takes the same
places as the command line. Servers: any SFTP server, SFTP-only and chrooted accounts too. Linux, macOS, BSD, Termux.

Install: `rshm.pyz` anywhere on your `PATH`, or the deb, rpm, apk or Arch package of a
[release](https://github.com/maulusck/rshm/releases) (`/usr/bin/rshm`; needs python3 and the ssh client).

## Keys

mc's. `F1`, `F9` and `rshm --help` list them all, from the one table (`KEYS` in `ui.py`) that runs them. Prompts edit
like a shell line, `Tab` completes paths here or on a host (`Shift+Tab` back). Mouse: tap selects, tap again opens.

## How it behaves

- **Copy, move**: from any pane to any pane, here or on a host, as `cp -pP` (mode, times, links kept). A move is a
  rename where it can be, else a copy, then the source goes (another disk, a USB stick). Each file lands as
  `NAME.rshm-part`, renamed when whole; a dropped link reconnects and resumes. Replacing asks, file by file: `y` `n`, `a` all, `s` none,
  `u` where newer.
- **Archives** (zip, jar, tar, tar.gz, tar.bz2, tar.xz, and one file as .gz .bz2 .xz): `Enter` browses one, `F5` copies out of it. `P` packs the
  marked entries into the other pane, the kind by the name you give; on an archive, `P` unpacks it there (one entry
  at its top lands as itself, more go into a folder named after it). Corruption is reported, never passed on.
- **Links**: a link to a directory opens under its own name; `..` goes back where you came from, as `cd` does.
- **Long operations** show after half a second: progress, speed, time left. `Space` pauses, `Esc` stops.
- **Logins**: one per host, shared by both panes and all transfers. ssh asks on the terminal (keys, agent, passwords,
  `ProxyCommand`), once. Everything about reaching a host is `~/.ssh/config`'s.
- **View, edit**: `[open]` match first, then your tools on a private local copy (sent back only if changed), else the
  built-in viewer (less keys) and editor (nano keys).
- **Quit**: `q`, `F10` or `^C` ask; the same key again quits. `^Z` suspends (`fg` resumes). While something runs, `^C`
  stops it, as `Esc` does.

## Config

Optional; rshm writes none until `F4` on the host list. Found as `-c FILE`, `rshm.toml` beside `rshm.pyz`, or
`$XDG_CONFIG_HOME/rshm.toml` (`~/.config`). Needs Python 3.11 to be read.

```toml
theme = "default"                         # default matrix amber frutiger teletext avionics
                                          # robot cyberwave steel paper ember random
include = []                              # files beside this one; their [hosts] are added

[tools]                                   # unset: $VISUAL, $EDITOR, $PAGER, else built-in
edit = ""                                 # "nvim", "code -w {file}"
view = ""                                 # "less -R", "bat --paging=always"

[open]                                    # Enter on a file: first matching pattern's command
# "*.pdf *.djvu" = "zathura {file}"

[commands]                                # F2 menu, run in the pane's directory: {name} {names} {dir} {other}
# "disk usage" = "du -sh {names}"
# "ACLs" = "getfacl {names}"

[hosts]                                   # ssh destinations, as in ~/.ssh/config
# work = "work"
# pi = { host = "me@pi", path = "/srv" }
# lab = ["box", "nas"]
# [hosts.prod]
# web = "deploy@web.example.com"
```

## Limits

Owners are not copied (`A` sets them); pipes, sockets and devices are skipped; a plain `.tar` has no checksum. No
sudo: use a host alias that logs in as root. Shells and `[commands]` on a host need a POSIX shell. SFTP v3 names
only three errors; the rest is "failed on the server". Every theme meets WCAG contrast; terminals with fewer colours,
no colour, no UTF-8 or an unknown `TERM` get what they can show.

## Code and tests

`config.py` config and hosts · `ssh.py` logins · `sftp.py` SFTP v3 client · `fs.py` file systems (`Local`, `Remote`,
`Archive`), copy, pack · `ui.py` the screen, `KEYS`, `THEMES` · `view.py` `edit.py` `screen.py`.

`python3 test_rshm.py` (logic, against a real `sftp-server`), `test_stress.py` (random trees, drops, full disks,
fuzzing), `test_app.py` (the app on a strict virtual terminal, then a monkey), `test_live.py` (the built `rshm.pyz` on a
real terminal and a throwaway sshd; needs `pyte`, `sshd`). As root (they chown). Every push runs them on Python 3.9
to 3.15; a tag named as the version then releases `rshm.pyz` and the packages (`nfpm.yaml`,
`.github/workflows/release.yml`). The version is said once, in `rshm/__init__.py`.
