"""fleet commands. Usage: fleet <command> [args]   (run `fleet help`)

Every command takes a Ctx, which holds the system layer (tmux, git, wt), the clock and the output
streams, so the tests can drive commands against fakes.
"""

import io
import json
import os
import re
import shlex
import sys
import tempfile
import time

from . import args as A
from . import lifecycle as lc
from . import screen as S
from . import state as st
from .errors import FleetError, FleetExit
from .system import Git, Tmux, Wt

DELAY_DEFAULT = 0.02  # seconds between typed characters
TYPED_TIMEOUT = 2.0  # how long typed text may take to show up in the prompt box
PROMPT_TIMEOUT = 60  # how long a new claude may take to show its prompt box
CTRL_ENTER = "\x1b[13;5u"  # Ctrl+Enter as CSI-u: interrupt the turn and run the prompt now

HELP = """\
fleet up [name] [--detach] [-- claude args]   create tmux session fleet[-name], orchestrator claude in window 0 'orch'
    --mode M             orchestrator permission mode (default auto)
fleet new <repo|repo/main> <branch> [opts]     worker window <branch> in worktree <repo>/<branch> (reused if present;
                                               else new branch from fetched origin default), made with wt; its hooks
                                               run in the foreground before claude starts. Exit 3: folder-trust dialog.
    --no-worktree        run in <dir> as given; <branch> is then just the worker name
    --base REF           branch from REF instead of origin's default branch
    --mode M             permission mode (default auto)
    --prompt-file P      once ready, send "Read P and do what it says."
    --prompt-stdin       the same, with stdin saved to a file under the state dir
    -- args              extra claude args
fleet status [--json]                          every worker: state (busy|blocked|done|idle|exited), age, detail, last reply
fleet send [--now] [--force] <worker> <text>   type text like a human, verify it landed, press Enter
                                               --now: Ctrl+Enter (interrupts a running turn, runs immediately)
                                               --force: send even though the prompt box holds typed text
                                               --prompt-file P (instead of <text>): send "Read P and do what it says."
                                               --prompt-stdin (instead of <text>): the same, with stdin saved to a file
                                               under the state dir. Both keep newlines; use them for long prompts.
                                               Use only one of <text>, --prompt-file and --prompt-stdin.
fleet broadcast [--to w1,w2] [--now] [--force] [--wait SECS] <text | --prompt-file P | --prompt-stdin>
                                               send to every tracked worker (or only --to); a refusal skips that
                                               worker, not the rest. --wait: wait for all, print each reply.
                                               Exit 1 if any send failed, else 4 if any blocked, 2 if any timed out
fleet wait <worker> [timeout_s]                block until done/idle (exit 0), blocked (exit 4) or timeout (exit 2)
fleet last <worker> [n]                        last n assistant replies from the transcript (default 1)
fleet read <worker> [lines]                    the screen plus <lines> of scrollback (default 60)
fleet keys <worker> <tmux-key>...              raw keys for dialogs: Enter, Escape, Up, Down, BTab, 1, 2...
fleet rm <worker> [--force]                    kill the window; remove its worktree if clean (--force discards
                                               changes); the branch is always kept
Workers are addressed by window name. Session: $FLEET_SESSION, else the fleet session you are in, else the only one.
"""

STATUS_GLYPH = ("#{?#{==:#{@fleet_state},blocked},#[fg=red]⚠#[default],"
                "#{?#{==:#{@fleet_state},busy},⋯,"
                "#{?#{==:#{@fleet_state},done},#[fg=green]✓#[default],}}}")


class Ctx:
    def __init__(self, tmux=None, git=None, wt=None, environ=None, out=None, err=None,
                 now=None, sleep=None, monotonic=None, fleet_path="", hook_path="", isatty=None,
                 cwd=None, stdin=None):
        self.tmux = tmux or Tmux()
        self.git = git or Git()
        self.wt = wt or Wt()
        self.environ = os.environ if environ is None else environ
        self.out = out or sys.stdout
        self.stdin = stdin or sys.stdin
        self.err = err or sys.stderr
        self.now = now or (lambda: int(time.time()))
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        self.fleet_path = fleet_path
        self.hook_path = hook_path
        self.isatty = isatty if isatty is not None else (lambda: sys.stdout.isatty())
        self.cwd = cwd or os.getcwd
        self.store = st.Store(st.state_dir(self.environ))
        self.lifecycle = lc.Lifecycle(self.store, lambda: self.now(), glyph=self._glyph)
        self.delay = float(self.environ.get("FLEET_DELAY") or DELAY_DEFAULT)

    def quiet(self):
        """The same context with stdout discarded (broadcast runs send and wait this way)."""
        c = Ctx.__new__(Ctx)
        c.__dict__.update(self.__dict__)
        c.out = io.StringIO()
        return c

    def _glyph(self, pane, value):
        self.tmux.set_window_option(pane, "@fleet_state", value, check=False)

    def say(self, *lines):
        for l in lines:
            print(l, file=self.out)
        self.out.flush()

    def warn(self, *lines):
        for l in lines:
            print(l, file=self.err)
        self.err.flush()


# ---------- session and targets ----------

def is_fleet_session(name):
    return name == "fleet" or name.startswith("fleet-")


def session(ctx):
    """The fleet session this command acts on."""
    s = ctx.environ.get("FLEET_SESSION", "")
    if s:
        return s
    pane = ctx.environ.get("TMUX_PANE", "")
    if ctx.environ.get("TMUX") and pane:
        try:
            cur = ctx.tmux.session_of(pane)
        except FleetError:
            cur = ""
        if is_fleet_session(cur):
            return cur
    found = [x for x in ctx.tmux.sessions() if is_fleet_session(x)]
    if not found:
        raise FleetError("no fleet session running; start one with: fleet up [name]")
    if len(found) > 1:
        raise FleetError("several fleet sessions running (%s); set FLEET_SESSION=<session>" % " ".join(found))
    return found[0]


def target(ctx, name):
    """Worker name (window name in the fleet session) -> pane id. A raw pane id ("%12") is accepted
    too, but only for a pane of the fleet session. Never returns an empty id, the caller's own pane,
    or an orchestrator's (any pane marked @fleet_role=orch)."""
    if not name:
        raise FleetError("no worker name given")
    s = session(ctx)
    panes = ctx.tmux.panes(s)
    match = [p for p in panes if p["window_name"] == name]
    if not match and re.match(r"^%\d+$", name):
        match = [p for p in panes if p["pane_id"] == name]
    if not match or not match[0]["pane_id"]:
        raise FleetError("no worker '%s' in fleet session '%s'" % (name, s))
    pane, role = match[0]["pane_id"], match[0]["@fleet_role"]
    own = ctx.environ.get("TMUX_PANE", "")
    if own and pane == own:
        raise FleetError("'%s' resolves to this pane (the orchestrator), not a worker" % name)
    if role == "orch":
        raise FleetError("'%s' is the orchestrator's pane, not a worker" % name)
    return pane


def tracked(ctx, name):
    """(pane, state file path, record) of a worker that has hook state."""
    pane = target(ctx, name)
    path = ctx.store.path(pane)
    rec = st.Store.load(path)
    if rec is None:
        raise FleetError("'%s' is not a tracked worker (no hook state)" % name)
    return pane, path, rec


def capture(ctx, pane):
    return S.Screen(ctx.tmux.capture(pane, ansi=True), ansi=True)


# ---------- state ----------

def reconcile(ctx, pane):
    """Correct a stale busy/blocked state from the screen (hooks miss interrupts and cancelled
    dialogs). Returns the record as it stands afterwards."""

    def look():
        try:
            return capture(ctx, pane).classify() if pane else None
        except FleetError:
            return None

    def recheck():
        ctx.sleep(lc.RECHECK_DELAY)
        return look()

    return ctx.lifecycle.reconcile(pane, look, recheck)


def sweep(ctx):
    """Drop state files whose pane is gone (or was reused by something else)."""
    live = ctx.tmux.all_panes()
    if live is None:
        return  # tmux could not be asked: that is not evidence that panes are gone
    for f in ctx.store.files():
        rec = st.Store.load(f)
        if rec is None:
            continue
        if live.get(rec.get("pane", ""), None) != rec.get("name"):
            st.Store.remove(f)


def session_records(ctx, s):
    ctx.store.ensure()
    sweep(ctx)
    res = []
    for f in ctx.store.files():
        rec = st.Store.load(f)
        if rec is not None and rec.get("session") == s:
            res.append((f, rec))
    return res


def worker_names(ctx):
    return [rec.get("name", "") for _, rec in session_records(ctx, session(ctx)) if rec.get("name")]


def style_window(ctx, target_):
    """Status-bar glyph from @fleet_state; window-scoped, so the user's global config is untouched."""
    t = ctx.tmux
    t.set_window_option(target_, "window-status-format", "#I:#W" + STATUS_GLYPH + " ")
    t.set_window_option(target_, "window-status-current-format", "#[bold]#I:#W#[nobold]" + STATUS_GLYPH + "*")
    t.set_window_option(target_, "allow-rename", "off")
    t.set_window_option(target_, "automatic-rename", "off")


# ---------- commands ----------

def cmd_up(ctx, argv):
    o = A.parse_up(argv)
    s = "fleet-" + o["name"] if o["name"] else "fleet"
    if ctx.tmux.has_session(s):
        raise FleetError("session '%s' already exists. Attach with: tmux attach -t %s" % (s, s))
    cwd = ctx.cwd()
    pane = ctx.tmux.new_session(s, "orch", cwd)
    if not pane:
        raise FleetError("tmux did not report the new session's pane")
    style_window(ctx, pane)
    ctx.tmux.set_pane_option(pane, "@fleet_role", "orch")
    cmd = ["claude", "/fleet-orchestrator", "--name", s, "--permission-mode", o["mode"],
           "--allowedTools", "Bash(%s *)" % ctx.fleet_path] + o["extra"]
    ctx.tmux.send_literal(pane, _shell_line(cmd, ctx), orch_ok=True)
    ctx.tmux.send_keys(pane, "Enter", orch_ok=True)
    if not o["detach"] and not ctx.environ.get("TMUX") and ctx.isatty():
        ctx.tmux.attach(s)
    ctx.say("fleet '%s' up: orchestrator in window 0 (%s)." % (s, cwd))
    if ctx.environ.get("TMUX"):
        ctx.say("Switch with: tmux switch-client -t %s" % s)
    else:
        ctx.say("Attach with: tmux attach -t %s" % s)
    return 0


def _shell_line(argv, ctx):
    """A command line for the new pane's shell. FLEET_STATE_DIR is passed on when set: panes inherit
    the tmux server's environment, not ours."""
    line = " ".join(shlex.quote(a) for a in argv)
    sd = ctx.environ.get("FLEET_STATE_DIR")
    if sd:
        line = "FLEET_STATE_DIR=%s %s" % (shlex.quote(sd), line)
    return line


def hooks_settings(hook_path, state_root):
    """The --settings file content: every hook event runs fleet-hook, told where the state lives."""
    cmd = "FLEET_STATE_DIR=%s %s" % (shlex.quote(state_root), shlex.quote(hook_path))
    return {"hooks": {ev: [{"hooks": [{"type": "command", "command": "%s %s" % (cmd, ev)}]}]
                      for ev in lc.EVENTS}}


def resolve_worktree(ctx, src, name, base):
    """Find or make the worktree for branch `name` of the repo at `src` (<repo> or <repo>/main).
    Returns (dir, repo)."""
    if not valid_branch_name(ctx, name):
        raise FleetError("'%s' is not a valid branch name" % name)
    if os.path.basename(src) == "main" and os.path.exists(os.path.join(src, ".git")):
        repo = os.path.dirname(src)
    elif os.path.exists(os.path.join(src, "main", ".git")):
        repo = src
    else:
        raise FleetError("%s does not follow <repo>/main (no git checkout at <repo>/main). "
                         "Pass --no-worktree to run there without a worktree." % src)
    main = os.path.join(repo, "main")
    # Worktrees go through worktrunk (wt): its worktree-path template decides where they live,
    # and its hooks (e.g. a post-start build) make them usable before claude starts.
    if not ctx.wt.available():
        raise FleetError("wt (worktrunk) not found; install it, or pass --no-worktree")
    existing = _wt_path_of(ctx.wt.list_json(main), name)
    if existing:
        ctx.say("reusing worktree %s" % existing)
        return existing, repo
    if not ctx.git.ok(main, "fetch", "--quiet", "origin"):
        ctx.warn("warning: git fetch failed; using local refs")
    create, how = False, ""
    if ctx.git.ok(main, "show-ref", "--verify", "--quiet", "refs/heads/" + name):
        how = "from existing branch %s" % name
    elif not base and ctx.git.ok(main, "show-ref", "--verify", "--quiet", "refs/remotes/origin/" + name):
        how = "tracking origin/%s" % name  # wt switch creates the local tracking branch
    else:
        if not base:
            base = default_base(ctx, main)
        create, how = True, "on new branch %s from %s" % (name, base)
    # --no-hooks, then run them here: wt switch would start post-start hooks in the background,
    # and claude must not start (and spawn its MCP servers) before they finish.
    rc, out = ctx.wt.switch(main, name, create=create, base=base)
    if rc != 0:
        raise FleetError("wt switch failed for %s" % name)
    d = _switch_path(out)
    if not d or not os.path.isdir(d):
        raise FleetError("wt switch did not report the new worktree's path: %s" % out.strip())
    ctx.say("worktree %s %s" % (d, how))
    for h in ("pre-start", "post-start", "post-switch"):
        ctx.say("running wt %s hooks..." % h)
        if not ctx.wt.hook(d, h):
            ctx.warn("warning: wt %s hooks failed in %s" % (h, d))
    return d, repo


def valid_branch_name(ctx, name):
    rc, _ = ctx.git.run(".", "check-ref-format", "--branch", name)
    return rc == 0


def default_base(ctx, main):
    b = ctx.git.out(main, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if b:
        return b
    for cand in ("origin/main", "origin/master"):
        if ctx.git.ok(main, "rev-parse", "--verify", "--quiet", cand):
            return cand
    raise FleetError("cannot find origin's default branch; pass --base REF")


def _wt_path_of(list_json, branch):
    try:
        data = json.loads(list_json) if list_json.strip() else {}
    except ValueError:
        return ""
    items = data.get("items", []) if isinstance(data, dict) else data
    for it in items or []:
        if isinstance(it, dict) and it.get("branch") == branch:
            p = (it.get("worktree") or {}).get("path")
            if p:
                return p
    return ""


def _switch_path(out):
    try:
        d = json.loads(out)
    except ValueError:
        return ""
    if not isinstance(d, dict):
        return ""
    return d.get("path") or d.get("worktree_path") or (d.get("worktree") or {}).get("path") or ""


def await_prompt(ctx, pane, name, dirname):
    """Wait for claude's prompt box in `pane`. Exit 3 on the folder-trust dialog."""
    scr = None
    deadline = ctx.monotonic() + PROMPT_TIMEOUT
    while ctx.monotonic() < deadline:
        ctx.sleep(1)
        scr = capture(ctx, pane)
        if scr.is_trust_dialog():
            raise FleetExit(3, "trust-prompt: claude asks whether to trust %s. Ask the user; on yes run: "
                               "fleet keys %s Down Enter" % (dirname, name))
        if scr.is_ready():
            return
    if scr is not None:
        ctx.say(*scr.tail(20))
    raise FleetError("claude did not show a prompt box within %ds (screen above)" % PROMPT_TIMEOUT)


def cmd_new(ctx, argv):
    o = A.parse_new(argv)
    name, src, pfile = o["name"], o["src"], o["prompt_file"]
    if "/" in name:
        raise FleetError("'%s' contains '/': worktrees live at <repo>/<branch>, so names must be flat" % name)
    if name == "orch":
        raise FleetError("'orch' is the orchestrator's window")
    if re.match(r"^%\d+$", name) or name.startswith("-"):
        raise FleetError("'%s' cannot be a worker name" % name)
    if pfile:
        pfile = prompt_path(pfile)
    if not os.path.isdir(src):
        raise FleetError("no such directory: %s" % src)
    if o["prompt_stdin"]:
        pfile = stdin_prompt(ctx)  # read now: stdin is not there to read after a minute of wt hooks
    src = os.path.realpath(src)

    s = session(ctx)
    if any(p["window_name"] == name for p in ctx.tmux.panes(s)):
        raise FleetError("a window named '%s' already runs in '%s'" % (name, s))

    repo = wt = ""
    if not o["worktree"]:
        d = src
    else:
        d, repo = resolve_worktree(ctx, src, name, o["base"])
        d = os.path.realpath(d)
        if d != os.path.join(repo, "main"):
            wt = d

    ctx.store.ensure()
    hooks = os.path.join(ctx.store.root, "hooks.json")
    st.Store.write(hooks, hooks_settings(ctx.hook_path, ctx.store.root))

    pane = ctx.tmux.new_window(s, name, d)
    if not re.match(r"^%\d+$", pane or ""):
        raise FleetError("tmux did not report the new window's pane id (got %r)" % pane)
    ctx.tmux.set_pane_option(pane, "@fleet_name", name)
    style_window(ctx, pane)
    ctx.lifecycle.create(pane, name=name, session=s, dir=d, repo=repo, worktree=wt,
                         branch=name if repo else "", mode=o["mode"])

    cmd = ["claude", "/fleet-worker", "--name", name, "--permission-mode", o["mode"], "--settings", hooks] + o["extra"]
    ctx.tmux.send_literal(pane, _shell_line(cmd, ctx))
    ctx.tmux.send_keys(pane, "Enter")
    try:
        await_prompt(ctx, pane, name, d)
    except (FleetError, FleetExit):
        if pfile:
            ctx.warn('prompt not sent; once the worker is ready run: fleet send %s "%s"'
                     % (name, read_prompt_file(pfile)))
        raise
    ctx.say("ready: worker '%s' in %s (mode %s). Attach: tmux attach -t %s, then select window '%s'"
            % (name, d, o["mode"], s, name))
    if pfile:
        return send(ctx, name, read_prompt_file(pfile))
    return 0


def send(ctx, name, text, now=False, force=False):
    pane = target(ctx, name)
    scr = capture(ctx, pane)
    if scr.has_dialog():
        ctx.say(*scr.tail(12))
        raise FleetError("'%s' shows a dialog (above); answer it with: fleet keys %s <key>" % (name, name))
    box = scr.prompt_box()
    if box is None:
        ctx.say(*scr.tail(12))
        raise FleetError("no claude prompt box on '%s' (screen above); not typing" % name)
    if box.typed and not force:
        raise FleetError("prompt box already holds: '%s' (someone typed there). "
                         "Re-run with --force to append anyway." % box.typed)
    # A grey suggested prompt (box.ghost) is not text anyone typed: the first typed key replaces it.
    for ch in text:
        ctx.tmux.send_literal(pane, ch)
        ctx.sleep(ctx.delay)
    deadline = ctx.monotonic() + TYPED_TIMEOUT
    while True:
        ctx.sleep(0.3)
        scr = capture(ctx, pane)
        if S.shows_typed(scr, text):
            break
        if ctx.monotonic() >= deadline:
            ctx.say(*scr.tail(8))
            raise FleetError("typed text not visible in the prompt box; not submitting (screen above)")
    if now:
        ctx.tmux.send_literal(pane, CTRL_ENTER)
    else:
        ctx.tmux.send_keys(pane, "Enter")
    if ctx.lifecycle.is_tracked(pane):
        ctx.lifecycle.prompt_sent(pane)
    ctx.say("sent%s to %s: %s" % (" (now)" if now else "", name, text))
    return 0


def read_prompt_file(path):
    return "Read %s and do what it says." % path


def stdin_prompt(ctx):
    """Save stdin to a new file under the state dir and return its path. Workers are told to read
    the file instead of having the text typed in: typing is slow for long prompts and flattens
    newlines. The file is kept, since a worker reads it after fleet has exited."""
    if ctx.stdin.isatty():
        raise FleetError("--prompt-stdin: stdin is a terminal; pipe the prompt in "
                         "(e.g. fleet ... --prompt-stdin < prompt.md)")
    text = ctx.stdin.read()
    if not text.strip():
        raise FleetError("--prompt-stdin: stdin is empty; nothing to send")
    d = os.path.join(ctx.store.root, "prompts")
    os.makedirs(d, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="prompt-%d-" % ctx.now(), suffix=".md", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o644)
    return path


def prompt_path(path):
    """The absolute path of an existing prompt file: workers run in other directories."""
    if not os.path.isfile(path):
        raise FleetError("no such prompt file: %s" % path)
    return os.path.abspath(path)


def prompt_text(ctx, o):
    """What to type for a parsed send/broadcast: the text itself, or an instruction to read the
    --prompt-file or the file --prompt-stdin was saved to."""
    if o["prompt_file"]:
        return read_prompt_file(prompt_path(o["prompt_file"]))
    if o["prompt_stdin"]:
        return read_prompt_file(stdin_prompt(ctx))
    return o["text"]


def cmd_send(ctx, argv):
    o = A.parse_send(argv)
    return send(ctx, o["name"], prompt_text(ctx, o), now=o["now"], force=o["force"])


def wait(ctx, name, timeout):
    """0 when done/idle/exited, 4 when blocked, 2 on timeout."""
    pane, path, rec = tracked(ctx, name)
    deadline = ctx.monotonic() + timeout
    while True:
        rec = reconcile(ctx, pane)
        if rec is None:
            raise FleetError("'%s' is no longer tracked (its state file is gone)" % name)
        s = lc.wait_state(rec)
        if s in lc.WAIT_FINAL:
            ctx.say(s)
            return 0
        if s == "blocked":
            ctx.say("blocked: %s. See it with: fleet read %s" % (rec.get("detail", ""), name))
            return 4
        if ctx.monotonic() >= deadline:
            ctx.say("still %s after %ds" % (s, timeout))
            return 2
        ctx.sleep(1)


def cmd_wait(ctx, argv):
    o = A.parse_wait(argv)
    return wait(ctx, o["name"], o["timeout"])


def cmd_broadcast(ctx, argv):
    o = A.parse_broadcast(argv)
    workers = o["to"] or worker_names(ctx)
    if not workers:
        raise FleetError("no workers to send to")
    text = prompt_text(ctx, o)  # resolved once: with --prompt-stdin every recipient reads one file
    sent, rc = [], 0
    for w in workers:
        try:
            send(ctx.quiet(), w, text, now=o["now"], force=o["force"])
        except FleetError as e:
            ctx.warn("fleet: %s" % e)
            ctx.say("NOT sent to %s (reason above)" % w)
            rc = 1
        else:
            sent.append(w)
            ctx.say("sent to %s" % w)
    if o["wait"] is None:
        return rc
    # Workers run concurrently, so waiting on each in turn costs no more than the slowest.
    blocked = timed_out = False
    for w in sent:
        try:
            wr = wait(ctx.quiet(), w, o["wait"])
        except FleetError as e:
            ctx.warn("fleet: %s" % e)
            wr = 1
        if wr == 0:
            ctx.say("##### %s: done" % w)
            last(ctx, w, 1)
        elif wr == 4:
            ctx.say("##### %s: BLOCKED, see it with: fleet read %s" % (w, w))
            blocked = True
        elif wr == 2:
            ctx.say("##### %s: still busy after %ss" % (w, o["wait"]))
            timed_out = True
        else:
            ctx.say("##### %s: wait failed (%s)" % (w, wr))
            rc = 1
    return broadcast_code(rc != 0, blocked, timed_out)


def broadcast_code(failed, blocked, timed_out):
    """1 if any send or wait failed, else 4 if any worker blocked, else 2 if any timed out."""
    if failed:
        return 1
    if blocked:
        return 4
    if timed_out:
        return 2
    return 0


def cmd_read(ctx, argv):
    o = A.parse_read(argv)
    pane = target(ctx, o["name"])
    text = ctx.tmux.capture(pane, scrollback=o["n"])
    for line in text.split("\n"):
        if line:
            ctx.say(line)
    return 0


TRANSCRIPT_LAG = 2.0  # how long the transcript may trail the Stop hook


def last(ctx, name, n):
    _, _, rec = tracked(ctx, name)
    tp = rec.get("transcript_path") or ""
    if not tp or not os.path.isfile(tp):
        ctx.say(rec.get("last_message") or "(no reply yet)")
        return 0
    replies = settled_replies(ctx, tp, rec)
    picked = replies[-n:] if n > 0 else []
    if not picked:
        ctx.say("(no reply yet)")
        return 0
    for r in picked:
        ctx.say(r, "----")
    return 0


def settled_replies(ctx, tp, rec):
    """The transcript's replies, once it holds the reply the Stop hook reported. The Stop hook can
    fire before Claude Code has written that reply to the transcript, so right after `fleet wait` the
    transcript's last reply may still be the previous turn's. Re-read for a moment; if it never
    shows up, use the hook's copy."""
    want = (rec.get("last_message") or "").strip() if rec.get("event") == "Stop" else ""
    deadline = ctx.monotonic() + TRANSCRIPT_LAG
    while True:
        replies = transcript_replies(tp)
        if not want or any(_same_reply(want, r) for r in replies[-3:]):
            return replies
        if ctx.monotonic() >= deadline:
            return replies + [rec["last_message"]]
        ctx.sleep(0.2)


def _same_reply(hook_text, transcript_text):
    a, b = S.compact(hook_text), S.compact(transcript_text)
    return bool(a) and bool(b) and (a in b or b in a)


def transcript_replies(path):
    """The text of each assistant message in a Claude Code transcript (JSONL), in order."""
    res = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("type") != "assistant":
                continue
            content = (e.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                res.append("\n".join(texts))
    return res


def cmd_last(ctx, argv):
    o = A.parse_last(argv)
    return last(ctx, o["name"], o["n"])


def cmd_keys(ctx, argv):
    o = A.parse_keys(argv)
    pane = target(ctx, o["name"])
    ctx.tmux.send_keys(pane, *o["keys"])
    ctx.lifecycle.dialog_answered(pane)
    return 0


def age(seconds):
    d = max(0, int(seconds))
    if d < 60:
        return "%ds" % d
    if d < 3600:
        return "%dm" % (d // 60)
    return "%dh" % (d // 3600)


def _is_claude_command(cmd):
    return "claude" in cmd or re.match(r"^\d+\.\d+\.\d+$", cmd) is not None


def cmd_status(ctx, argv):
    o = A.parse_status(argv)
    s = session(ctx)
    recs = []
    for _, found in session_records(ctx, s):
        rec = reconcile(ctx, found["pane"]) if found.get("pane") else found
        if rec is not None:
            recs.append(rec)
    untracked = [p["window_name"] for p in ctx.tmux.panes(s)
                 if not p["@fleet_name"] and not p["@fleet_role"] and _is_claude_command(p["pane_current_command"])]
    if o["json"]:
        ctx.say(json.dumps({"workers": recs, "untracked": untracked}, indent=2, ensure_ascii=False))
        return 0
    ctx.say("fleet session: %s" % s)
    if not recs and not untracked:
        ctx.say("(no workers)")
        return 0
    row = "%-24s %-8s %-5s %-22s %s"
    ctx.say(row % ("WORKER", "STATE", "AGE", "DETAIL", "LAST MESSAGE"))
    now = ctx.now()
    for r in recs:
        msg = re.sub(r"\s+", " ", r.get("last_message") or "")
        ctx.say(row % (r.get("name", ""), r.get("state", ""), age(now - (r.get("ts") or 0)),
                       (r.get("detail") or "")[:22], msg[:80]))
    for u in untracked:
        ctx.say("%-24s %-8s" % (u, "untracked"))
    return 0


def cmd_rm(ctx, argv):
    o = A.parse_rm(argv)
    name = o["name"]
    if name == "orch":
        raise FleetError("refusing to remove the orchestrator")
    pane = target(ctx, name)
    path = ctx.store.path(pane)
    rec = st.Store.load(path) or {}
    wt, repo, branch = rec.get("worktree") or "", rec.get("repo") or "", rec.get("branch") or ""
    if wt and os.path.isdir(wt) and not o["force"]:
        rc, porcelain = ctx.git.run(wt, "status", "--porcelain")
        if rc != 0:
            raise FleetError("cannot read git status of %s; not removing it (--force to discard anyway)" % wt)
        if porcelain.strip():
            _, short = ctx.git.run(wt, "status", "--short")
            ctx.say(*short.splitlines()[:20])
            raise FleetError("uncommitted changes in %s (above). Commit them, or re-run with --force to discard them." % wt)
    ctx.tmux.kill_window(pane)
    st.Store.remove(path)
    ctx.say("killed window '%s'" % name)
    if wt and os.path.isdir(wt):
        # wt remove runs the remove hooks; --no-delete-branch keeps the branch, --foreground so it is gone on return.
        if not ctx.wt.remove(os.path.join(repo, "main"), wt, force=o["force"]):
            raise FleetError("wt remove failed for %s" % wt)
        ctx.say("removed worktree %s; branch %s kept" % (wt, branch))
    return 0


COMMANDS = {
    "up": cmd_up, "new": cmd_new, "status": cmd_status, "send": cmd_send, "wait": cmd_wait,
    "read": cmd_read, "last": cmd_last, "keys": cmd_keys, "rm": cmd_rm, "broadcast": cmd_broadcast,
}


def run(ctx, argv):
    """Run one command; returns the exit code. Errors become `fleet: <message>` on stderr."""
    cmd = argv[0] if argv else "help"
    fn = COMMANDS.get(cmd)
    if fn is None:
        ctx.out.write(HELP)
        return 0
    try:
        return fn(ctx, argv[1:])
    except FleetExit as e:
        if e.message:
            ctx.warn(e.message)
        return e.code
    except FleetError as e:
        ctx.warn("fleet: %s" % e)
        return e.code


def main(argv, fleet_path):
    here = os.path.dirname(fleet_path)
    ctx = Ctx(fleet_path=fleet_path, hook_path=os.path.join(here, "fleet-hook"))
    try:
        return run(ctx, argv)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 1
