# rshm

Two-pane file manager for this host and every host in `~/.ssh/config`, over sftp. Midnight Commander keys and
layout. Needs Python 3.9+ and OpenSSH; no other dependencies. Viewer, editor and archive reader are built in.
Runs as one file, with or without a config.

```sh
python3 rshm.pyz              # this host, and the host list
python3 rshm.pyz web          # web in pane 2: name, group/name or unique part
python3 rshm.pyz web db       # one host per pane
python3 -m rshm --pyz FILE    # write rshm as a single executable file
```

Linux, macOS, BSD, Termux. Servers: any SFTP server (OpenSSH, Windows OpenSSH, Dropbear with sftp-server,
SFTP-only and chrooted accounts).

## Screen

```
 Pane 1   File   Mark   Command   Options   Pane 2  me@box   menu bar (F9, or tap); user@host
┌─ 1 local:/home/me 28G free ──┬─ 2 web:/srv/app ─────────┐ pane, host:directory, marks, filter, free space
 ..                            │ ..                        long names cut in the middle, extension kept
 src/                          │ static/                   ls -F: / directory @ link * executable | fifo = socket
 latest@ -> v2.1/              │ app.py               12K  symbolic links show their target
*notes.md                 4.1K │ config.toml           1K  colour by file type, per theme
└─ -rw-r--r-- 1 me:me 4.1K 2026-10-14 16:22 notes.md ─────┘ entry under the cursor, as ls -l
 16:22:05 ✓ copied notes.md to web:/srv/app  4.1K in 0s    log: one line per operation
1help    2menu    3view    4edit    5copy  …  10quit         F-key bar (keys or taps)
```

A pane shows a directory, here or on a host, or the host list (`@`, or tap the pane title). Panes stand side by side,
stacked on a tall terminal, one at a time (`Tab`) when two do not fit. An operation running over half a second shows
as the newest log line and in a box in the middle of the screen; Space pauses, Esc stops.

## Keys

`F1` and `rshm --help` list every key by menu, from the same table as the menus and the F-key bar.

| Key | Action |
|---|---|
| `Enter` `→` `l` · `←` `h` `Bksp` | open (directory, `[open]` match, archive, else view) · parent directory |
| `F3` `v` · `F4` `e` · `i` | view · edit (on the host list: the config) · information |
| `F5` `c` · `F6` `m` | copy · move to the other pane (mode and times kept, links kept as links) |
| `F7` `n` · `F8` `x` `Del` · `R` | mkdir · delete · rename |
| `A` · `L` · `H` · `z` | mode, owner, time · symbolic link (on a link: change target) · hard link · disk usage |
| `Space` `Ins` · `*` · `+` `-` · `D` | mark · mark all / none · mark / unmark by pattern · mark differences |
| `S` · `.` · `/` · `^F` | sort (name, extension, size, date, reverse) · dotfiles · filter · find by name |
| `b` `f` · `g` · `~` · `o` · `^U` | history back / forward · go to directory · home · same directory in other pane · swap panes |
| `F2` · `^O` `s` · `p` | menu (also right-click; your `[commands]` last) · shell · check host |
| `Tab` · `@` · `Esc` · `r` · `t` · `[` `]` · `^L` · `F10` `q` | other pane · host list · cancel · reread · theme · scroll log · redraw · quit |

Prompts edit like a shell line: `←` `→` `Home` `End` `Del` `^A` `^E` `^K` `^U` `^W`. A suggested value is selected:
typing replaces it, `Bksp` clears it, `Enter` accepts it. `Tab` completes paths, here or on a host. Forms: `↑` `↓`
`Tab` between fields. Mouse: click selects, click again opens; right-click opens the menu; the wheel scrolls.
Viewer: less keys (`space` `b` `g` `G` `/` `?` `n` `N`, `N g` line N, `w` wrap, `x` hex). Editor: nano keys (`^S`
save, `^X` exit, `^W` find, `^\` replace, `^K` `^U` cut, paste, `^_` line, `^Z` undo).

## Config

Optional. Without one, rshm uses built-in defaults and writes nothing. `F4` on the host list creates it from the
default below. Search order: `-c FILE`, `rshm.toml` beside `rshm.pyz`, `$XDG_CONFIG_HOME/rshm.toml`
(`~/.config`). Read from Python 3.11 (TOML); on 3.9 and 3.10 it is ignored, with a note in the log.

```toml
theme = "default"                         # default matrix amber frutiger teletext avionics
                                          # robot cyberwave steel paper ember
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

## Behaviour

| Area | |
|---|---|
| Copy, move | between any two panes: local and host, host and host (through this host), within a host (server-side on OpenSSH 9.0+), out of archives. As `cp -pP`: mode and times kept, links copied as links. Directories merge; replacing asks first. A move within one file system is a rename |
| Transfers | byte-exact. Each file is written as `NAME.rshm-part` and renamed when complete: the target is the old file or the new, never half. A replaced file takes the source's mode and times; the owner is whoever copies. The source is opened before the target is created. A lost connection reconnects and resumes; a part is resumed only if its last bytes match the source |
| Log | one line per operation: `✓ copied notes.md to web:/srv  4.1K in 0s, 1.2M/s`, `✗ deleting old/ in web:/srv: old/x: Permission denied`, `✗ copying a.iso to local:/tmp: stopped`. Copy, move, delete, mkdir, rename, link, attributes, save; also find, compare, size, check host. Navigation and viewing are not logged. 500 lines; `[` `]` or the wheel scroll |
| Progress | none under half a second. Beyond: the operation's log line, live (`copying a.iso to local:/tmp  1.2G of 4.0G  80M/s  36s left`), and a box over the screen: what, bar, numbers, `[ Space pause ]` `[ Esc stop ]`. Space pauses (the box at once), Esc or `^C` stops. Keys typed meanwhile run after a short operation, not after a long one. A stopped copy leaves no part file; files already copied stay |
| Attributes | `A` on the marked entries or the one under the cursor; only changed fields are applied; names or numbers |
| View, edit | `[open]` match first. Text goes to your tools (`[tools]`, else `$VISUAL` `$EDITOR` `$PAGER`) as a private local copy, written back only if changed. Binary files go to the built-in hex viewer only. Built-in viewer: first MB of any file. Built-in editor: text up to 1 MB |
| Archives | zip, jar, tar, tar.gz, tar.bz2, tar.xz: browsed as directories, read-only, nested. A remote archive is copied here first and removed on exit. Corruption is reported, never passed on (each format's checksum) |
| Links | `L` and `H` ask for the new name (other pane's directory offered) for the entry under the cursor. Symbolic links are relative. `i` shows the link count of a file |
| Logins | one per host, shared by both panes and all transfers. Keys, agent or `SSH_ASKPASS` first, else ssh prompts. The terminal is handed over for any prompt (gpg-agent's pinentry included) and the screen redrawn after |
| Exit | quit, `kill` or a closed terminal: logins closed, temporary copies removed |

## Terminals

| Terminal | rshm |
|---|---|
| UTF-8 (first of `LC_ALL`, `LC_CTYPE`, `LANG` set) | line drawing, ✓ ✗, arrows; otherwise ASCII as mc: `+-\|` frames, `+` `!`, `~` for cut names, `?` |
| 256 colours | all themes (`t`); the choice is saved |
| 8 or 16 colours | default theme in the terminal's colours; the saved choice is kept |
| no colours, or `NO_COLOR` set | bold, reverse, dim |
| `TERM` without terminfo here (kitty, ghostty over ssh) | drawn as `xterm-256color`, noted in the log |
| no cursor addressing (`TERM=dumb`), or not a terminal | refused |

Themes share one structure (`THEMES` in `ui.py`): page, text, faint, accent, cursor, bar, error, ok and one colour
per file type. Every theme meets WCAG contrast: 4.5:1 for text, 3:1 for faint text.

## Limits

| Limit | |
|---|---|
| shell, `[commands]` on a host | need a POSIX shell; browsing and transfers do not |
| copies | owners not kept (`A` sets them); hard links within a tree become separate files; pipes, sockets and devices are skipped (a move keeps such a source) |
| ACLs, SELinux labels | through F2 (`getfacl`, `chcon`) |
| `NAME.rshm-part` | reserved: a file of that name beside `NAME` is taken as a partial copy |
| plain `.tar` | no checksum: corruption inside is not detected |
| find | names only; for contents, `grep` through F2 or the shell |
| `kill -9` | logins remain until 5 minutes idle |
| `^C` during a login | waits for ssh's timeout (8 s) |
| root-only files | no sudo; use a host alias that logs in as root |
| errors on a host | SFTP v3 names only "no such file", "permission denied", "unsupported"; anything else (disk full, quota, read-only) is "failed on the server" |
| not UTF-8 | non-ASCII names show as `?` |

## Code

| Module | |
|---|---|
| `config.py` | config and host list (its tree plus `~/.ssh/config`) |
| `ssh.py` | logins: one OpenSSH master per host |
| `sftp.py` | SFTP v3 client |
| `fs.py` | file systems: `Local`, `Remote`, `Archive`; copy, delete, save, walk |
| `ui.py` | the screen; `KEYS`: keys, menus, F-keys, help; `THEMES`: colours |
| `view.py` `edit.py` `screen.py` | viewer, editor; text width, fitting, ASCII fallback |

To change a key: edit its row in `KEYS`, then `python3 -m rshm --pyz rshm.pyz`. To add an action: a method on `App`
and a row in `KEYS`. To add a file system: a class with `fs.py`'s primitives.

## Tests

| Run | |
|---|---|
| `python3 test_rshm.py` | logic, offline: config, hosts, layout, text width, file types, copies in every direction, links, times, attributes, archives, find, sort, viewer, editor, themes |
| `python3 test_stress.py` | against a real `sftp-server`, seeded (`RSHM_SEED`, `RSHM_TREES`): random trees copied every way and compared with `cp -a`; connection dropped and `^C` before every reply; full disk; shrinking source; servers that refuse attributes, print banners or read short; fuzzed text, editor, wire format, config |
| `python3 test_app.py` | the application on a strict virtual terminal (`RSHM_MONKEY`): every branch, then random keys, taps and sizes on every terminal kind; confined to a temporary directory |
| `python3 test_live.py` | the built `rshm.pyz` on a real terminal: every key, menu and tap, every screen at every size, terminal kinds, then hosts through a throwaway sshd and a jump host (needs `pyte`, `sshd`; `RSHM_TERM` sets the terminal) |

All lines run under test (stdlib `trace`), on Python 3.11 to 3.13. On 3.9 and 3.10, everything that needs no config.
