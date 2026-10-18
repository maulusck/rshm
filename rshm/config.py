"""rshm's own config and the hosts it shows: its tree plus every ~/.ssh/config alias. All of it optional:
with no config rshm runs on its built-ins and writes nothing."""
import fnmatch, glob, os, re, shlex, tomllib
from pathlib import Path

SSH_CONFIG = Path.home() / ".ssh" / "config"
SAMPLE = """\
theme = "default"                         # default matrix amber frutiger teletext avionics
                                          # robot cyberwave steel paper ember
include = []                              # more files beside this one, whose [hosts] join these

[tools]                                   # empty: $VISUAL, $EDITOR, $PAGER, else rshm's own
edit = ""                                 # "nvim", "code -w {file}"
view = ""                                 # "less -R", "bat --paging=always"

[open]                                    # Enter on a file: the first matching pattern runs its command
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
"""


def locate():  # rshm.toml next to rshm.pyz (it travels with it), else $XDG_CONFIG_HOME (~/.config)
    pyz = Path(__file__).resolve().parents[1]
    if pyz.suffix == ".pyz" and (pyz.parent / "rshm.toml").exists():
        return pyz.parent / "rshm.toml"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "rshm.toml"


def read(cfg):  # the config; none at all is fine. include: more files (beside it) whose [hosts] join its own
    c = tomllib.loads(cfg.read_text()) if cfg.exists() else {}
    for f in c.get("include") if isinstance(c.get("include"), list) else ():
        more = tomllib.loads((cfg.parent / Path(f).expanduser()).read_text()).get("hosts")
        if isinstance(more, dict) and isinstance(c.get("hosts", {}), dict):
            c["hosts"] = {**more, **c.get("hosts", {})}  # a name in both: yours wins
    return c


def create(cfg):  # only when asked for (`e` in the host list): the commented sample to start from
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        save(cfg, SAMPLE)


def save(path, text):  # atomic (a crash can't leave half a file), private all the way, symlinks kept
    path = path.resolve()
    tmp = path.with_name(f".{path.name}.rshm-tmp")
    tmp.unlink(missing_ok=True)
    try:
        tmp.touch(0o600)
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:  # disk full, ^C: no half-written temp file left behind
        tmp.unlink(missing_ok=True)
        raise


def retheme(txt, name):  # swap just the theme value; comments and layout stay
    new, n = re.subn(r"""^(theme\s*=\s*)(["']).*?\2""", rf'\g<1>"{name}"', txt, count=1, flags=re.M)
    return new if n else f'theme = "{name}"\n' + txt


def table(c, name):  # a [section] of name = "command" strings; anything else in it is ignored
    t = c.get(name)
    return {k: v for k, v in t.items() if isinstance(v, str)} if isinstance(t, dict) else {}


ENV = {"edit": ("VISUAL", "EDITOR"), "view": ("PAGER",)}


def tool(c, what):  # your [tools] choice, else the environment's, else None: rshm's own
    return table(c, "tools").get(what) or next((os.environ[v] for v in ENV[what] if os.environ.get(v)), None)


def opener(c, name):  # the [open] command for a file name, None if no pattern matches
    return next((cmd for pats, cmd in table(c, "open").items()
                 if any(fnmatch.fnmatch(name.lower(), pat.lower()) for pat in pats.split())), None)


def cmdline(template, path):  # a tool's command for one file, for sh -c: {file} quoted, else appended
    q = shlex.quote(path)
    return template.replace("{file}", q) if "{file}" in template else f"{template} {q}"


def hosts(c):  # rshm's hosts, plus a live group of ~/.ssh/config aliases not listed here: nothing to import
    hs = tree(c.get("hosts"))
    try:
        known = {h["host"] for *_, h, _ in flat(hs, set()) if h}
        extra = [a for a in ssh_hosts(SSH_CONFIG) if a not in known] if SSH_CONFIG.exists() else []
    except (OSError, ValueError):  # an unreadable ssh config must not take rshm down with it
        extra = []
    return {**hs, "~/.ssh/config": tree(extra)} if extra else hs


def tree(node):  # every host a table {"host": destination, ...}, once, so per-host state (last folder) sticks to it
    if isinstance(node, list):  # a list is a group of destinations, each named as itself
        node = {x: x for x in node if isinstance(x, str)}
    out = {}
    for k, v in (node.items() if isinstance(node, dict) else ()):
        v = {"host": v} if isinstance(v, str) else v
        ok = isinstance(v, dict) and isinstance(v.get("host"), str)  # a host: its destination and path, text only
        out[k] = {x: v[x] for x in ("host", "path") if isinstance(v.get(x), str)} if ok else tree(v)
    return out


def ssh_hosts(path, seen=()):  # Host aliases of an ssh config, Includes followed, patterns skipped
    out = []
    for line in path.read_text().splitlines():
        m = re.match(r"(\w+)\s*[=\s]\s*(.+)", line.strip())
        k, v = (m[1].lower(), m[2]) if m else ("", "")
        if k == "include" and path not in seen:
            for pat in shlex.split(v):
                for f in sorted(glob.glob(str(SSH_CONFIG.parent / Path(pat).expanduser()))):
                    out += ssh_hosts(Path(f), (*seen, path))
        elif k == "host":
            out += [a for a in shlex.split(v) if not any(c in a for c in "*?!")]
    return list(dict.fromkeys(out))


def flat(node, closed, depth=0, path=()):  # the tree (from tree()) as rows: (depth, name, host or None, path)
    for k, v in node.items():
        p = path + (k,)
        if isinstance(v.get("host"), str):  # a host; a group may hold one named "host"
            yield depth, k, v, p
        else:
            yield depth, k, None, p
            if p not in closed:
                yield from flat(v, closed, depth + 1, p)


def groups(hosts):  # every group's path: the tree starts with all of them folded
    return {p for _, _, h, p in flat(hosts, set()) if not h}


def match(row, q):  # q already lowercase; "" matches all
    return any(q in x.lower() for x in row[3]) or bool(row[2]) and q in row[2]["host"].lower()


def find(hosts, name):  # -> [(path, host)]: exact name or group/name, else substring: `rshm we` -> prod/web
    hs = [(p, h) for _, _, h, p in flat(hosts, set()) if h]
    exact = [x for x in hs if name in ("/".join(x[0]), x[0][-1])]
    return exact or [x for x in hs if name.lower() in "/".join(x[0]).lower()]
