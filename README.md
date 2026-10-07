# rshm

Remote SHell Manager: your SSH hosts in one curses screen. Host tree on the left, SFTP files on the right, activity log
at the bottom; keyboard or mouse. One Python file, standard library only.

```sh
pip install .
rshm                 # open it; ? lists every key
rshm web             # ssh straight in: name, group/name, or any unique part of it
rshm -c work.toml    # another config file
```

Linux, macOS, BSD or Termux; Python 3.11+, OpenSSH 8.4+, a UTF-8 terminal. Servers: anything that speaks SFTP
(OpenSSH, Windows OpenSSH, Dropbear with sftp-server, SFTP-only and chrooted accounts).

## Config

`~/.config/rshm.toml`, created on first run. A host is an ssh destination; everything about reaching it (user, port,
key, jump, password) belongs in `~/.ssh/config`. Its hosts also show up on their own, in a live `~/.ssh/config` group.

```toml
theme = "default"                      # default | matrix | amber | frutiger | teletext (`t` cycles and saves)

[hosts]
work = "work"                          # name = destination: an ssh alias, host or user@host
lab = ["pi", "box"]                    # a group of destinations, each named as itself

[hosts.prod]                           # a table is a group; nest as deep as you like
web = "deploy@web.example.com"
db = { host = "db", path = "/srv/db" } # path: the folder to start in
```

## Behaviour

| Area | What it does |
|---|---|
| Tree | groups start folded; hosts outside any group always show |
| Filter | `/` filters the pane you're in (hosts and files each their own); `⏎` applies it and selects the first match, never opens it; `Esc` clears it |
| Logins | one per host, reused until rshm exits; tried quietly first (keys, agent, your `SSH_ASKPASS`), else ssh asks in the terminal |
| Live check | `p`: one round trip to the host through ssh, by the same route as a session, with the time it took |
| Files | all SFTP, byte for byte, files and folders both ways; each file travels as `NAME.rshm-part` and is renamed into place when complete; permissions travel with it; a dropped link reconnects and resumes; `Ctrl-C` stops, starting again resumes |
| View, edit | on a private local copy, saved back only if changed; a failed save keeps the copy and says where; over 50 MB or a binary asks first |
| Exit | quitting, `kill` or a closed terminal ends every login rshm started; on disk stays only the config |

## Limits

| Limit | Note |
|---|---|
| `s` (shell here) | needs a POSIX shell on the server; browsing and transfers need none |
| saving an edit | replaces the file: it then belongs to your login user; hard links, ACLs and SELinux labels don't carry over |
| resumed transfer | trusts its `.rshm-part`: if the source changed meanwhile, delete it first |
| skipped in transfers | pipes, sockets, devices, symlinked folders inside a tree |
| `kill -9` | logins linger until 5 idle minutes pass |

## Tests

| Run | Checks |
|---|---|
| `python3 test_rshm.py` | the logic, offline |
| `python3 test_live.py` | everything against a throwaway sshd on a local port, as you, removed afterwards |
