# rshm

Remote SHell Manager: a two-pane file manager, Midnight Commander style, for this machine and every host in your
`~/.ssh/config`. It needs Python 3.11+ and `ssh`, nothing else: viewer, editor and archive browsing are built in.
One file on a USB stick runs on any machine as is; your config makes it yours (your hosts sorted, bat, nvim, 7z).

```sh
python3 rshm.pyz              # or ./rshm.pyz; F1 lists every key, F9 opens the menus
python3 rshm.pyz web          # web in pane 2: a name, group/name, or any unique part of it
python3 rshm.pyz web db       # one host in each pane: copy straight between them
```

Copy it into your PATH as `rshm` for a plain command (or `pip install .`). rshm makes the single file of itself,
from wherever it runs (this source, a pip install, a `.pyz`): `python3 -m rshm --pyz rshm.pyz`.

Linux, macOS, BSD or Termux, a UTF-8 terminal, OpenSSH 8.4+. Servers: anything that speaks SFTP (OpenSSH,
Windows OpenSSH, Dropbear with sftp-server, SFTP-only and chrooted accounts).

## Screen

```
 Pane 1   File   Mark   Command   Options   Pane 2  me@box   menus: F9 or a tap; one open at a time, lit; who and where
┌─ 1 local:/home/me 28G free ──┬─ 2 web:/srv/app ─────────┐ pane number, host:folder, marks, filter, free space
 ..                            │ ..                        names cut in the middle, the extension kept;
 src/                          │ static/                   ls -F marks: / folder @ link * program | fifo = socket
 latest@ -> v2.1/              │ app.py               12K  a link shows where it points
*notes.md                 4.1K │ config.toml           1K  colour by kind, from the theme
└─ -rw-r--r-- 1 me:me 4.1K 2026-10-14 16:22 notes.md ─────┘ the entry under the cursor, ls -l style
 16:22:05 ✓ → notes.md  4.1K in 0s                         the log: wheel or [ ] scroll it
1help    2menu    3view    4edit    5copy  …  10quit         mc's F-keys, in mc's slots: press or tap
```

Each pane shows a folder, here or on a host, or the host list (`@`, or tap its title). The panes sit side by side,
one above the other when the terminal stands upright (a phone, Termux), one at a time (`Tab`) when two don't fit.
`i` shows everything about an entry, a name too long for the screen included.

## Keys

`F1` and `rshm --help` list them all, from the one table every key, menu and F-key comes from.

| Key | Does |
|---|---|
| `⏎` `→` `l` · `←` `h` `⌫` | open: folder, your `[open]` match, archive, else view · up (out of an archive, out of find results) |
| `F3` `v` · `F4` `e` · `i` | view · edit (on the host list: the config) · info |
| `F5` `c` · `F6` `m` | copy · move to the other pane (mode and times travel; links stay links) |
| `F7` `n` · `F8` `x` `Del` · `R` | new folder · delete · rename |
| `A` · `L` · `H` · `z` | attributes: mode, owner, time, one row each · symlink to the entry, where you say (on a link: where it points) · hard link, where you say · size |
| `Space` `Ins` · `*` · `+` `-` · `D` | mark · all / none · by pattern · what differs from the other pane |
| `S` · `.` · `/` · `^F` | sort: name, extension, size, date, reverse · dotfiles · filter (`⏎` applies) · find |
| `b` `f` · `g` · `~` · `o` · `^U` | history back / forward · go to · home · other pane here · swap panes |
| `F2` · `^O` `s` · `p` | menu of what's under the cursor, your `[commands]` last · shell here (on the host list: on that host) · live check (never asks for a login) |
| `Tab` · `@` · `Esc` · `r` · `t` · `[` `]` · `^L` · `F10` `q` | other pane · host list · back: filter, marks, find results, host list; stops what's running · reload · theme · log · draw the screen again · quit |

Every question line edits like a shell's: `←` `→` `Home` `End` `Del`, `^A` `^E` `^K` `^U` `^W`; in a form, `↑` `↓` `Tab`
move between rows. Mouse and touch: tap selects, a second tap opens (a host too); right-click opens the
menu of what's under it (`F2`); the wheel scrolls panes, the log, menus, viewer and editor. The viewer has less's keys (`space` `b` `g` `G`
`/` `?` `n` `N`, `N g` for line N, `w` wrap, `x` hex), the editor nano's (`^S` save, `^X` exit, `^W` find,
`^\` replace, `^K` `^U` cut and paste lines, `^_` line, `^Z` undo).

## Config

None needed: without one rshm runs on its built-ins and writes nothing. `F4` on the host list creates it from this
default, which shows every option. It is the first of: `-c FILE`, `rshm.toml` next to `rshm.pyz` (it travels with
it), `$XDG_CONFIG_HOME/rshm.toml` (`~/.config`).

```toml
theme = "default"                         # default matrix amber frutiger teletext
include = []                              # more files beside this one, whose [hosts] join these

[tools]                                   # empty: $VISUAL, $EDITOR, $PAGER, else rshm's own
edit = ""                                 # "nvim", "code -w {file}"
view = ""                                 # "less -R", "bat --paging=always"

[open]                                    # ⏎ on a file: the first matching pattern runs its command
# "*.pdf *.djvu" = "zathura {file}"

[commands]                                # F2, in the pane's folder: {name} {names} {dir} {other}
# "disk usage" = "du -sh {names}"
# "ACLs" = "getfacl {names}"

[hosts]                                   # ssh destinations; reaching them is ~/.ssh/config's business
# work = "work"
# pi = { host = "me@pi", path = "/srv" }
# lab = ["box", "nas"]
# [hosts.prod]
# web = "deploy@web.example.com"
```

## Behaviour

| Area | What it does |
|---|---|
| Copy, move | between any two panes: here ↔ host, host ↔ host (through this machine), within one host (by the server itself on OpenSSH 9.0+), out of archives; like `cp -pP`: mode and times travel, links stay links; a folder into a folder merges, asking first if anything gets replaced; a move within one file system is a rename |
| Transfers | byte for byte; each file travels as `NAME.rshm-part`, renamed into place when complete, so a target is always whole: the old file or the new; a replaced file keeps its mode; the source is opened before anything is made at the other end (one you can't read leaves nothing there); a dropped link reconnects and resumes (a part is resumed only if its last bytes are the source's) |
| Stopping | `Esc` stops whatever runs (a copy, a fetch, a load, a find) at its next step, `Ctrl-C` at once; a stopped copy leaves no part behind, files already copied stay |
| Attributes | `A`: mode, owner, time of the marked entries (or the one under the cursor); only what you change is applied; names or numbers, on a host as its own listing shows them |
| View, edit | your `[open]` pattern if one matches; text to your tools (`[tools]`, else `$VISUAL` `$EDITOR` `$PAGER`), on a private local copy of a host's file (of what a link points to), saved back only if changed; a binary file never goes to an editor or a text tool: rshm's viewer shows it in hex. rshm's own viewer shows a file's first MB (any size, at once), its editor takes text up to a MB and saves straight back |
| Archives | zip, jar, tar, tar.gz / bz2 / xz open like folders, read-only, nested too; a host's is copied here first, removed on the way out; damage is an error, never silently wrong bytes (checked by each format's own checksum) |
| Links | `L` and `H` ask one thing, where the new name goes (the other pane's folder offered); the entry under the cursor is what it names. A symlink is relative (it still points right when the tree moves); a hard link is the same file under another name, so nothing marks it apart: `i` shows how many names a file has. A name already taken is said so |
| Logins | one per host, shared by both panes and every transfer; tried quietly first (keys, agent, your `SSH_ASKPASS`), else ssh asks; either way with the terminal handed over, so a passphrase prompt (gpg-agent's pinentry too) gets a clean screen and rshm's is drawn whole after |
| Exit | quitting, `kill` or a closed terminal ends every login rshm started and removes its copies |

## Limits

| Limit | Note |
|---|---|
| shell, `[commands]` on a host | need a POSIX shell there; browsing and transfers need none |
| copies | owners stay put (`A` sets them); hard links inside a tree become separate files; pipes, sockets and devices are never copied (a move that would leave them behind keeps its source) |
| ACLs, SELinux labels | through F2 (`getfacl`, `chcon`); `chattr` is the one door attributes use, should they come in |
| `NAME.rshm-part` | rshm's name for a file in transit: one of yours by that name, beside `NAME`, is taken for it |
| plain `.tar` | has no checksum: damage inside one can't be noticed (tar.gz, bz2, xz and zip have theirs) |
| find | names only; contents: `grep` through F2 or the shell |
| `kill -9` | logins linger until 5 idle minutes pass |
| `Ctrl-C` during a login | waits for ssh's own timeout (8 s): the terminal is the login's then |
| root-only files | as the user your ssh config logs in as: no sudo; a host alias logging in as root reaches them |

## Code

| Module | Does |
|---|---|
| `config.py` | the config and the host list (its tree plus `~/.ssh/config`) |
| `ssh.py` | logins: one OpenSSH master per host |
| `sftp.py` | the SFTP v3 client |
| `fs.py` | one file system interface: `Local`, `Remote`, `Archive`; copy, delete, save, walk across any of them; `kind()` |
| `ui.py` | the screen; `KEYS` is the one table behind keys, menus, F-keys, F1 and `--help`; `THEMES` colour by role |
| `view.py` `edit.py` `screen.py` | the built-in viewer and editor, and text measured and fitted in terminal cells |

A new action: a method on `App` and one row in `KEYS`. A new place to browse: a class with `fs.py`'s primitives.

## Tests

| Run | Checks |
|---|---|
| `python3 test_rshm.py` | the logic, offline: config and its default, hosts, layouts, text width, kinds, every copy direction, links, times, attributes, archives, find, compare, sort, viewer and editor |
| `python3 test_stress.py` | proof against a real `sftp-server`, seeded (`RSHM_SEED`, `RSHM_TREES`): random trees (odd names, sizes, modes, links, pipes) copied every way and held against `cp -a`; the link dropped before any reply, `Ctrl-C` before any reply, a full disk, a shrinking source, a server that won't set modes, prints a banner or reads short; text, editor, wire and config fuzzed |
| `python3 test_app.py` | the whole app in this process on a strict virtual terminal (a write past an edge fails): every branch the screen test doesn't press, then a monkey of random keys, taps and sizes (`RSHM_MONKEY`); jailed to a temp folder, no network |
| `python3 test_live.py` | the `rshm.pyz` it builds of itself, on a real screen in a terminal: every key, menus and taps, every screen through every size, the layouts as the terminal turns and shrinks; then hosts through a throwaway sshd and a jump. Any terminal (`RSHM_TERM=screen` …: keys from its terminfo). As you, in a temp folder removed afterwards (needs `pyte`; the host part needs `sshd`) |

Together they run every line of rshm (measured with the stdlib `trace`, the screen child included), on Python 3.11 to 3.13.
