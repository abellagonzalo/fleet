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
    usage = "usage: fleet up [name] [--detach] [--mode M] [-- claude args]"
    o = {"name": "", "detach": False, "mode": "auto", "extra": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--detach":
            o["detach"] = True
        elif a == "--mode":
            o["mode"] = _value(args, i, a)
            i += 1
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


NEW_USAGE = ("usage: fleet new <repo|repo/main> <branch> [<branch> ...] [--no-worktree] [--base REF] [--mode M] "
             "[--prompt-file P | --prompt-stdin] [-- claude args]")


def parse_new(args):
    """One worker per name in o["names"]; with several, no first prompt and no --no-worktree."""
    o = {"src": "", "names": [], "worktree": True, "base": "", "mode": "auto", "prompt_file": "",
         "prompt_stdin": False, "extra": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--no-worktree":
            o["worktree"] = False
        elif a == "--prompt-stdin":
            o["prompt_stdin"] = True
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
        else:
            o["names"].append(a)
        i += 1
    if not o["src"] or not o["names"]:
        raise UsageError(NEW_USAGE)
    if not o["mode"]:
        raise UsageError("--mode needs a value")
    if o["prompt_stdin"] and o["prompt_file"]:
        raise UsageError("use --prompt-file or --prompt-stdin, not both")
    if len(o["names"]) > 1:
        if o["prompt_stdin"] or o["prompt_file"]:
            raise UsageError("no first prompt with several branches: once the workers are ready, send each "
                             "worker its first prompt with fleet send")
        if not o["worktree"]:
            raise UsageError("--no-worktree takes one worker: several workers in one directory is not supported")
        for n in o["names"]:
            if o["names"].count(n) > 1:
                raise UsageError("'%s' is given twice" % n)
    return o


def parse_status(args):
    if not args:
        return {"json": False}
    if args == ["--json"]:
        return {"json": True}
    raise UsageError("usage: fleet status [--json]")


def parse_send(args):
    """Flags come before the worker; everything after it is the text (even words starting with -).
    With --prompt-file or --prompt-stdin the prompt comes from there, so nothing may follow the worker."""
    usage = "usage: fleet send [--now] [--force] [--prompt-file P | --prompt-stdin] <worker> [<text>]"
    o = {"now": False, "force": False, "prompt_file": "", "prompt_stdin": False, "name": "", "text": ""}
    i = 0
    while i < len(args) and args[i] in ("--now", "--force", "--prompt-stdin", "--prompt-file"):
        if args[i] == "--prompt-file":
            o["prompt_file"] = _value(args, i, args[i], "--prompt-file needs a path") or _no_path()
            i += 1
        else:
            o[args[i][2:].replace("-", "_")] = True
        i += 1
    if i < len(args) and args[i].startswith("-") and args[i] != "-":
        raise UsageError("unknown option %s; %s" % (args[i], usage))
    if i >= len(args):
        raise UsageError(usage)
    o["name"] = args[i]
    return _prompt(o, args[i + 1:], usage)


def parse_broadcast(args):
    usage = ("usage: fleet broadcast [--to w1,w2] [--now] [--force] [--wait SECS] "
             "[--prompt-file P | --prompt-stdin] [<text>]")
    o = {"now": False, "force": False, "prompt_file": "", "prompt_stdin": False, "to": [], "wait": None,
         "text": ""}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--now", "--force", "--prompt-stdin"):
            o[a[2:].replace("-", "_")] = True
        elif a == "--prompt-file":
            o["prompt_file"] = _value(args, i, a, "--prompt-file needs a path") or _no_path()
            i += 1
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
    return _prompt(o, args[i:], usage)


def _no_path():
    raise UsageError("--prompt-file needs a path")


ONE_PROMPT = "give the prompt one way only: as text, with --prompt-file P, or with --prompt-stdin"


def _prompt(o, words, usage):
    """Fill o["text"] from words, unless --prompt-file or --prompt-stdin supplies the prompt.
    Exactly one of the three may be used."""
    sources = [bool(words), bool(o["prompt_file"]), o["prompt_stdin"]]
    if sum(sources) > 1:
        raise UsageError(ONE_PROMPT)
    if o["prompt_file"] or o["prompt_stdin"]:
        return o
    if not words:
        raise UsageError(usage)
    o["text"] = _text(words)
    return o


def _text(words):
    text = " ".join(words).replace("\n", " ")
    if not text.strip():
        raise UsageError("nothing to send: the text is empty")
    return text


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
    usage = "usage: fleet rm <worker> [--force] [--keep-branch | -D/--force-delete]"
    o = {"force": False, "branch": "merged", "name": ""}
    keep = delete = False
    for a in args:
        if a == "--force":
            o["force"] = True
        elif a == "--keep-branch":
            keep = True
        elif a in ("-D", "--force-delete"):
            delete = True
        elif a.startswith("-"):
            raise UsageError("unknown option %s" % a)
        elif o["name"]:
            raise UsageError("unexpected argument %s" % a)
        else:
            o["name"] = a
    if not o["name"]:
        raise UsageError(usage)
    if keep and delete:
        raise UsageError("--keep-branch and -D (--force-delete) contradict each other")
    o["branch"] = "keep" if keep else "delete" if delete else "merged"
    return o
