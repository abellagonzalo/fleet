"""Argument parsing, one function per command, with the bash version's grammar (flags and positionals
mixed, `--` ending fleet's options, `send` taking flags only before the worker name).

Each returns a plain dict; a bad command line raises UsageError (exit 1, "fleet: <message>").
"""

from .errors import FleetError


class UsageError(FleetError):
    pass


def _value(args, i, flag, what=None):
    if i + 1 >= len(args):
        raise UsageError(what or "%s needs a value" % flag)
    return args[i + 1]


def _int(s, what):
    try:
        v = int(s)
    except (TypeError, ValueError):
        raise UsageError("%s must be a whole number, not %r" % (what, s))
    if v < 0:
        raise UsageError("%s must not be negative" % what)
    return v


def parse_up(args):
    usage = "usage: fleet up [name] [--detach] [-- claude args]"
    o = {"name": "", "detach": False, "extra": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--detach":
            o["detach"] = True
        elif a == "--":
            o["extra"] = args[i + 1:]
            break
        elif a.startswith("-"):
            raise UsageError("unknown option %s" % a)
        elif o["name"]:
            raise UsageError(usage)
        else:
            o["name"] = a
        i += 1
    return o


NEW_USAGE = ("usage: fleet new <repo|repo/main> <branch> [--no-worktree] [--base REF] [--mode M] "
             "[--prompt-file P] [-- claude args]")


def parse_new(args):
    o = {"src": "", "name": "", "worktree": True, "base": "", "mode": "auto", "prompt_file": "",
         "extra": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-worktree":
            o["worktree"] = False
        elif a in ("--base", "--mode", "--prompt-file"):
            o[a[2:].replace("-", "_")] = _value(args, i, a)
            i += 1
        elif a == "--":
            o["extra"] = args[i + 1:]
            break
        elif a.startswith("-"):
            raise UsageError("unknown option %s" % a)
        elif not o["src"]:
            o["src"] = a
        elif not o["name"]:
            o["name"] = a
        else:
            raise UsageError("unexpected argument %s" % a)
        i += 1
    if not o["src"] or not o["name"]:
        raise UsageError(NEW_USAGE)
    if not o["mode"]:
        raise UsageError("--mode needs a value")
    return o


def parse_status(args):
    if not args:
        return {"json": False}
    if args == ["--json"]:
        return {"json": True}
    raise UsageError("usage: fleet status [--json]")


def parse_send(args):
    """Flags come before the worker; everything after it is the text (even words starting with -)."""
    usage = "usage: fleet send [--now] [--force] <worker> <text>"
    o = {"now": False, "force": False, "name": "", "text": ""}
    i = 0
    while i < len(args) and args[i] in ("--now", "--force"):
        o[args[i][2:]] = True
        i += 1
    if i < len(args) and args[i].startswith("-") and args[i] != "-":
        raise UsageError("unknown option %s; %s" % (args[i], usage))
    if i + 1 >= len(args):
        raise UsageError(usage)
    o["name"] = args[i]
    o["text"] = " ".join(args[i + 1:]).replace("\n", " ")
    if not o["text"].strip():
        raise UsageError("nothing to send: the text is empty")
    return o


def parse_broadcast(args):
    usage = "usage: fleet broadcast [--to w1,w2] [--now] [--force] [--wait SECS] <text>"
    o = {"now": False, "force": False, "to": [], "wait": None, "text": ""}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--now", "--force"):
            o[a[2:]] = True
        elif a == "--to":
            v = _value(args, i, a, "--to needs a comma-separated list of workers")
            o["to"] = [w for w in v.split(",") if w]
            if not o["to"]:
                raise UsageError("--to needs a comma-separated list of workers")
            i += 1
        elif a == "--wait":
            o["wait"] = _int(_value(args, i, a, "--wait needs a timeout in seconds"), "--wait")
            i += 1
        elif a == "--":
            i += 1
            break
        elif a.startswith("-"):
            raise UsageError("unknown option %s" % a)
        else:
            break
        i += 1
    if i >= len(args):
        raise UsageError(usage)
    o["text"] = " ".join(args[i:]).replace("\n", " ")
    if not o["text"].strip():
        raise UsageError("nothing to send: the text is empty")
    return o


def _name_and_count(args, usage, default, what):
    if not args or not args[0] or len(args) > 2:
        raise UsageError(usage)
    n = _int(args[1], what) if len(args) > 1 else default
    return {"name": args[0], "n": n}


def parse_wait(args):
    o = _name_and_count(args, "usage: fleet wait <worker> [timeout_s]", 600, "timeout_s")
    return {"name": o["name"], "timeout": o["n"]}


def parse_read(args):
    return _name_and_count(args, "usage: fleet read <worker> [lines]", 60, "lines")


def parse_last(args):
    return _name_and_count(args, "usage: fleet last <worker> [n]", 1, "n")


def parse_keys(args):
    if len(args) < 2 or not args[0]:
        raise UsageError("usage: fleet keys <worker> <tmux-key>...")
    return {"name": args[0], "keys": args[1:]}


def parse_rm(args):
    o = {"force": False, "name": ""}
    for a in args:
        if a == "--force":
            o["force"] = True
        elif a.startswith("-"):
            raise UsageError("unknown option %s" % a)
        elif o["name"]:
            raise UsageError("unexpected argument %s" % a)
        else:
            o["name"] = a
    if not o["name"]:
        raise UsageError("usage: fleet rm <worker> [--force]")
    return o
