# rshm

Remote SHell Manager: a two-pane file manager, Midnight Commander style, for this machine and every host in your
`~/.ssh/config`. It needs Python 3.11+ and `ssh`, nothing else: viewer, editor and archive browsing are built in.
One file on a USB stick runs on any machine as is; your config makes it yours (your hosts sorted, bat, nvim, 7z).

```sh
python3 rshm.pyz              # or ./rshm.pyz; F1 lists every key
python3 rshm.pyz web          # web on the right: a name, group/name, or any unique part of it
python3 rshm.pyz web db       # one host in each pane: copy straight between them
```

Copy it into your PATH as `rshm` for a plain command (or `pip install .`). Build the single file with:

```sh
python3 -c "import zipapp; zipapp.create_archive('.', 'rshm.pyz', '/usr/bin/env python3', 'rshm.__main__:main', filter=lambda p: p.parts[0] == 'rshm' and p.suffix == '.py')"
```

Linux, macOS, BSD or Termux, a UTF-8 terminal, OpenSSH 8.4+. Servers: anything that speaks SFTP (OpenSSH,
Windows OpenSSH, Dropbear with sftp-server, SFTP-only and chrooted accounts).

## Keys

Each pane shows a folder, here or on a host, or the host list to pick one (`F9`). Letters do the same as F-keys.

| Key | Files | Host list |
|---|---|---|
| `⏎` `→` `l` | open: folder, archive, your `[open]` match, else view | open host here / fold group |
| `←` `h` `⌫` | parent folder (out of an archive at its top) | fold / to group |
| `F3` `v` · `F4` `e` | view · edit | · `e` edit config |
| `F5` `c` · `F6` `m` | copy · move to the other pane | |
| `F7` `n` · `F8` `x` `Del` · `R` · `A` | new folder · delete · rename · permissions | |
| `Space` `Ins` · `*` · `+` `-` | mark · all / none · mark / unmark by pattern | |
| `.` · `S` | dotfiles show / hide · sort by name, size, date | |
| `F2` | your `[commands]` | |
| `^O` `s` | shell in this folder | shell on the host |
| `g` · `~` | go to path · home | |
| `Tab` · `F9` `@` · `/` · `Esc` | other pane · host list · filter (`⏎` applies) · clear / back | same |
| `p` · `t` · `r` · `F10` `q` | live check · theme · reload · quit | same |

Viewer: `/` find, `n` `N` next / previous, `w` wrap, `x` hex / text, `q` back. Editor: `^S` save, `^Q` quit,
`^F` find, `^Z` undo. Mouse: click selects (a host opens at once, a file on the second click), right-click lists the
keys, the wheel scrolls.

## Config

None needed: without one rshm runs on its built-ins and writes nothing. `e` in the host list creates it from a
commented sample. It is the first of: `-c FILE`, `rshm.toml` next to `rshm.pyz` (it travels with it),
`$XDG_CONFIG_HOME/rshm.toml` (`~/.config`). Every part is optional:

```toml
theme = "amber"                        # default | matrix | amber | frutiger | teletext (`t` cycles)
include = ["stations.toml"]            # files beside this one whose [hosts] join yours: a generated list, say

[tools]                                # left out: $VISUAL / $EDITOR and $PAGER, else rshm's own
edit = "nvim"                          # {file} is the file; left out, it goes last
view = "bat --paging=always {file}"

[open]                                 # ⏎ on a file: the first matching pattern wins, before the built-ins
"*.pdf" = "zathura {file}"
"*.7z *.rar" = "7z l {file} | less"

[commands]                             # F2: run in the pane's folder, over ssh on a host
"disk usage" = "du -sh {names}"        # {name} cursor, {names} marked (else cursor), {dir}, {other}: shell-quoted

[hosts]                                # a host is an ssh destination; reaching it is ~/.ssh/config's business
work = "work"                          # name = destination: an ssh alias, host or user@host
lab = ["pi", "box"]                    # a group of destinations, each named as itself
[hosts.prod]                           # a table is a group; nest as deep as you like
db = { host = "db", path = "/srv/db" } # path: the folder to start in
```

`~/.ssh/config`'s own hosts show up too, in a live group of their own.

## Behaviour

| Area | What it does |
|---|---|
| Copy, move | between any two panes: here ↔ host, host ↔ host (through this machine), within one host (by the server itself on OpenSSH 9.0+), out of archives; a folder into a folder merges, asking first if anything gets replaced; a move within one file system is a rename |
| Transfers | byte for byte; each file travels as `NAME.rshm-part` and is renamed into place when complete; permissions travel with it, a replaced file keeps its own; a dropped link reconnects and resumes; `Ctrl-C` stops, starting again resumes |
| View, edit | built in: a host's file is read into memory and saved straight back, never written here; your tools get a private local copy, saved back only if changed (a failed save keeps it and says where); over 50 MB asks first |
| Archives | zip, jar, tar, tar.gz / bz2 / xz open like folders, read-only, nested too; a host's is copied here first, and removed on the way out |
| Logins | one per host, shared by both panes and every transfer; tried quietly first (keys, agent, your `SSH_ASKPASS`), else ssh asks in the terminal |
| Live check | `p`: one round trip to the host through ssh, by the same route as a session, with the time it took |
| Exit | quitting, `kill` or a closed terminal ends every login rshm started and removes its copies |

## Limits

| Limit | Note |
|---|---|
| shell, `[commands]` on a host | need a POSIX shell there; browsing and transfers need none |
| replacing a file | it then belongs to whoever copied it; hard links, ACLs and SELinux labels don't carry over |
| resumed transfer | trusts its `.rshm-part`: if the source changed meanwhile, delete it first |
| skipped in trees | pipes, sockets, devices, symlinked folders; links inside archives |
| built-in editor | a tab shows as `»`, wide characters as one cell; for more, set `[tools] edit` |
| `kill -9` | logins linger until 5 idle minutes pass |

## Code

| Module | Does |
|---|---|
| `config.py` | the config: hosts (its tree plus `~/.ssh/config`), tools, `[open]`, `[commands]` |
| `ssh.py` | logins: one OpenSSH master per host |
| `sftp.py` | the SFTP v3 client |
| `fs.py` | one file system interface: `Local`, `Remote`, `Archive`; copy, delete, save across any two |
| `ui.py` | the panes; `KEYS` is the one table behind dispatch, the F-key bar, the help menu and `--help` |
| `view.py` `edit.py` `screen.py` | the built-in viewer and editor, and what every screen shares |

A new action: a method on `App` and one row in `KEYS`. A new place to browse: a class with `fs.py`'s primitives.

## Tests

| Run | Checks |
|---|---|
| `python3 test_rshm.py` | the logic, every copy direction, archives, viewer and editor, offline |
| `python3 test_live.py` | `rshm.pyz` on a real screen in a terminal against a throwaway sshd on a local port, through a jump, as you, removed afterwards (needs `pyte`) |
