"""fleet commands. Usage: fleet <command> [args]   (run `fleet help`)

Every command takes a Ctx, which holds the system layer (tmux, a pane's Terminal, and git + wt behind
Worktrees), the clock and the output streams, so the tests can drive commands against fakes.
"""

import concurrent.futures
import io
import json
import os
import re
import shlex
import sys
import tempfile
import threading
import time

from . import args as A
from . import lifecycle as lc
from . import roster as R
from . import replies as rp
from . import screen as S
from . import state as st
from . import worktrees as W
from .errors import FleetError, FleetExit
from .system import DELAY_DEFAULT, Git, Tmux, Wt

TYPED_TIMEOUT = 2.0  # how long typed text may take to show up in the prompt box
PROMPT_TIMEOUT = 60  # how long a new claude may take to show its prompt box

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
                         Either way, a prompt whose first line is a slash command is typed as that
                         line, with the lines after it passed as "(also read <file> and follow it)"
    -- args              extra claude args
fleet new <repo|repo/main> <b1> <b2>... [opts] several workers at once: names checked and origin fetched before
                                               anything starts, then all provisioned and started in parallel
                                               (hook output prefixed [branch]). One line per branch: ready, failed
                                               or trust. Exit 1 if any failed, else 3 if any shows the trust
                                               dialog. --base, --mode and -- args apply to all; no --no-worktree,
                                               no prompt: send each worker its first prompt with fleet send.
fleet status [--json]                          every worker: state (starting|idle|busy|blocked|exited), outcome of
                                               its last turn (finished|interrupted), age, detail, last reply
fleet send [--now] [--force] <worker> <text>   type text like a human, verify it landed, press Enter
                                               --now: Ctrl+Enter (interrupts a running turn, runs immediately)
                                               --force: send even though the prompt box holds typed text
                                               --prompt-file P (instead of <text>): send "Read P and do what it says."
                                               --prompt-stdin (instead of <text>): the same, with stdin saved to a file
                                               under the state dir. Both keep newlines; use them for long prompts.
                                               A slash command on the first line is typed as-is (see new).
                                               Use only one of <text>, --prompt-file and --prompt-stdin.
fleet broadcast [--to w1,w2] [--now] [--force] [--wait SECS] <text | --prompt-file P | --prompt-stdin>
                                               send to every tracked worker (or only --to); a refusal skips that
                                               worker, not the rest. --wait: wait for all, print each reply.
                                               Exit 1 if any send failed, else 4 if any blocked, 5 if any interrupted,
                                               2 if any timed out
fleet wait <worker> [timeout_s]                block until the turn after the last send finished (exit 0; also when
                                               exited, or idle if never sent to), was interrupted (exit 5), is
                                               blocked (exit 4), or timeout (exit 2)
fleet watch [--to w1,w2] [timeout_s]           for Claude Code's Monitor: one line per event of the watched workers
                                               (--to, else every tracked worker; fixed at start), flushed as it
                                               happens: "<w> finished: <first line of reply>", "<w> interrupted",
                                               "<w> blocked: <detail>. See it with: fleet read <w>", "<w> exited",
                                               "<w> gone" (no longer tracked; dropped). Every turn counts, whoever
                                               sent it. Exit 0 with "watch: all settled" once every watched worker
                                               is idle with no prompt left to start, or exited (blocked is not);
                                               exit 2 with "watch: timeout after Ns, still busy: ..." at timeout_s
                                               (default 1800); exit 1 on an unknown worker or no workers
fleet last <worker> [n]                        last n assistant replies from the transcript (default 1)
fleet read <worker> [lines]                    the screen plus <lines> of scrollback (default 60)
fleet keys <worker> <tmux-key>...              raw keys for dialogs: Enter, Escape, Up, Down, BTab, 1, 2...
fleet rm <worker> [--force] [--keep-branch | -D]
                                               kill the window; remove its worktree if clean (--force discards
                                               changes). Its branch goes, locally and on origin, when worktrunk sees
                                               it merged into the default branch (squash and rebase merges count);
                                               else it stays. --keep-branch: keep it anyway. -D (--force-delete):
                                               delete it even unmerged, locally only (origin's keeps any open PR)
Workers are addressed by window name. Session: $FLEET_SESSION, else the fleet session you are in, else the only one.
"""

STATUS_GLYPH = ("#{?#{==:#{@fleet_state},blocked},#[fg=red]⚠#[default],"
                "#{?#{==:#{@fleet_state},busy},⋯,"
                "#{?#{==:#{@fleet_state},finished},#[fg=green]✓#[default],"
                "#{?#{==:#{@fleet_state},interrupted},#[fg=yellow]↯#[default],}}}}")


class Ctx:
    def __init__(self, tmux=None, worktrees=None, environ=None, out=None, err=None,
                 now=None, sleep=None, monotonic=None, fleet_path="", hook_path="", isatty=None,
                 cwd=None, stdin=None):
        self.tmux = tmux or Tmux()
        self.worktrees = worktrees or W.Worktrees(Git(), Wt(), say=self.say, warn=self.warn)
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
        self.print_lock = threading.Lock()  # `new` with several branches prints from threads

    def terminal(self, pane, orchestrator_ok=False):
        """The Terminal of `pane`: the only way commands send it keys or read its screen.
        orchestrator_ok only for `fleet up` starting the orchestrator."""
        return self.tmux.terminal(pane, orchestrator_ok=orchestrator_ok, delay=self.delay, sleep=self.sleep)

    def quiet(self):
        """The same context with stdout discarded (broadcast runs send this way; its refusals carry their screen)."""
        c = Ctx.__new__(Ctx)
        c.__dict__.update(self.__dict__)
        c.out = io.StringIO()
        return c

    def _glyph(self, pane, value):
        self.tmux.set_window_option(pane, "@fleet_state", value, check=False)

    def say(self, *lines):
        with self.print_lock:
            for l in lines:
                print(l, file=self.out)
            self.out.flush()

    def warn(self, *lines):
        with self.print_lock:
            for l in lines:
                print(l, file=self.err)
            self.err.flush()


# ---------- session and targets ----------

def roster(ctx, session=None):
    """The Roster of the fleet session (found unless given), built once per command."""
    s = session or R.find_session(ctx.environ, ctx.tmux.session_of, ctx.tmux.sessions)
    panes = ctx.tmux.panes(s)
    tracked = {p["pane_id"] for p in panes if p["pane_id"] and ctx.lifecycle.is_tracked(p["pane_id"])}
    return R.Roster(s, panes, tracked, ctx.environ.get("TMUX_PANE", ""))


# ---------- state ----------

def screen_classifier(ctx, pane):
    """(look, recheck) for the lifecycle: what the pane's screen shows ('dialog' | 'busy' | 'quiet',
    None when unreadable), now and after a pause."""

    term = ctx.terminal(pane) if pane else None

    def look():
        try:
            return term.screen().classify() if term else None
        except FleetError:
            return None

    def recheck():
        ctx.sleep(lc.RECHECK_DELAY)
        return look()

    return look, recheck


def reconcile(ctx, pane):
    """Correct a stale busy/blocked state from the screen (hooks miss interrupts and cancelled
    dialogs). Returns the record as it stands afterwards."""
    return ctx.lifecycle.reconcile(pane, *screen_classifier(ctx, pane))


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
    term = ctx.terminal(pane, orchestrator_ok=True)
    term.type(_shell_line(cmd, ctx), paced=False)
    term.submit()
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


def await_prompt(ctx, pane, name, dirname):
    """Wait for claude's prompt box in `pane`. Exit 3 on the folder-trust dialog."""
    term, scr = ctx.terminal(pane), None
    deadline = ctx.monotonic() + PROMPT_TIMEOUT
    while ctx.monotonic() < deadline:
        ctx.sleep(1)
        scr = term.screen()
        if scr.is_trust_dialog():
            raise FleetExit(3, "trust-prompt: claude asks whether to trust %s. Ask the user; on yes run: "
                               "fleet keys %s Down Enter" % (dirname, name))
        if scr.is_ready():
            return
    raise FleetError("claude did not show a prompt box within %ds (screen above)" % PROMPT_TIMEOUT,
                     screen=scr.tail(20) if scr is not None else ())


def cmd_new(ctx, argv):
    o = A.parse_new(argv)
    if len(o["names"]) > 1:
        return new_several(ctx, o)
    name, src = o["names"][0], o["src"]
    ros = roster(ctx)
    ros.check_new_name(name)
    s = ros.session
    if not os.path.isdir(src):
        raise FleetError("no such directory: %s" % src)
    prompt = prompt_text(ctx, o)  # read now: stdin is not there to read after a minute of wt hooks
    src = os.path.realpath(src)

    if not o["worktree"]:
        d, repo, wt = src, "", ""
    else:
        d, repo, wt = provision(ctx.worktrees, src, name, o["base"])
    pane = launch(ctx, s, name, d, repo, wt, o, write_hooks(ctx))
    try:
        await_prompt(ctx, pane, name, d)
    except (FleetError, FleetExit):
        if prompt:
            ctx.warn("prompt not sent; once the worker is ready run: fleet send %s %s"
                     % (shlex.quote(name), shlex.quote(prompt[0])))
        raise
    ctx.say("ready: worker '%s' in %s (mode %s). Attach: tmux attach -t %s, then select window '%s'"
            % (name, d, o["mode"], s, name))
    if prompt:
        text, form = prompt
        return send(ctx, roster(ctx, session=s).lookup(name), text, form=form)
    return 0


def new_several(ctx, o):
    """`fleet new <repo> <b1> <b2> ...`: refuse before starting anything if any name can't be a new
    worker, fetch origin once, then provision and launch every worker concurrently. One result line
    per branch; exit 1 if any failed, else 3 if any shows the folder-trust dialog."""
    names, src = o["names"], o["src"]
    ros = roster(ctx)
    for name in names:
        ros.check_new_name(name)
    if not os.path.isdir(src):
        raise FleetError("no such directory: %s" % src)
    src = os.path.realpath(src)
    for name in names:
        ctx.worktrees.check_branch(name)
    ctx.worktrees.fetch(src)  # once: concurrent fetches of one repo race on its ref locks
    hooks = write_hooks(ctx)

    def start(name):
        wts = ctx.worktrees.labelled(name)
        try:
            d, repo, wt = provision(wts, src, name, o["base"], fetch=False)
            await_prompt(ctx, launch(ctx, ros.session, name, d, repo, wt, o, hooks), name, d)
            return "ready", "ready: worker '%s' in %s" % (name, d)
        except FleetExit as e:
            if e.code != 3:
                return "failed", "failed: worker '%s': %s" % (name, e.message or "exit %d" % e.code)
            return "trust", ("trust: worker '%s' is showing the folder-trust dialog; answer with fleet keys %s "
                             "Down Enter" % (name, name))
        except FleetError as e:
            ctx.say(*("[%s] %s" % (name, l) for l in e.screen))  # the screen the message calls "above"
            return "failed", "failed: worker '%s': %s" % (name, e)
        except Exception as e:  # a bug in one worker's start must not lose the others' results
            return "failed", "failed: worker '%s': %s: %s" % (name, type(e).__name__, e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as pool:
        results = list(pool.map(start, names))
    ctx.say(*(line for _, line in results))
    kinds = [k for k, _ in results]
    if "ready" in kinds:
        ctx.say("Attach: tmux attach -t %s, then select a worker's window" % ros.session)
    return 1 if "failed" in kinds else 3 if "trust" in kinds else 0


def provision(wts, src, name, base, fetch=True):
    """The worktree of branch `name` (found or made by `wts`): (dir, repo, worktree), where worktree
    is "" when the dir is the repo's main checkout."""
    d, repo = wts.ensure(src, name, base, fetch=fetch)
    d = os.path.realpath(d)
    return d, repo, "" if d == W.main_checkout(repo) else d


def write_hooks(ctx):
    """Write the --settings file every worker's claude gets; returns its path."""
    ctx.store.ensure()
    hooks = os.path.join(ctx.store.root, "hooks.json")
    st.Store.write(hooks, hooks_settings(ctx.hook_path, ctx.store.root))
    return hooks


def launch(ctx, s, name, d, repo, wt, o, hooks):
    """Open worker `name`'s window in session `s`, track it and start claude in `d`. Returns the pane;
    the caller waits for the prompt box (await_prompt)."""
    pane = ctx.tmux.new_window(s, name, d)
    if not re.match(r"^%\d+$", pane or ""):
        raise FleetError("tmux did not report the new window's pane id (got %r)" % pane)
    ctx.tmux.set_pane_option(pane, "@fleet_name", name)
    style_window(ctx, pane)
    ctx.lifecycle.create(pane, name=name, session=s, dir=d, repo=repo, worktree=wt,
                         branch=name if repo else "", mode=o["mode"])

    cmd = ["claude", "/fleet-worker", "--name", name, "--permission-mode", o["mode"], "--settings", hooks] + o["extra"]
    term = ctx.terminal(pane)
    term.type(_shell_line(cmd, ctx), paced=False)
    term.submit()
    return pane


def send(ctx, w, text, now=False, force=False, form=""):
    """Type `text` into the prompt box of `w` (a Roster handle) and submit it. `form`: how the text
    was made from a prompt (see typed_prompt), shown in the output line."""
    pane, name = w.pane, w.name
    term = ctx.terminal(pane)
    scr = term.screen()
    if scr.has_dialog():
        raise FleetError("'%s' shows a dialog (above); answer it with: fleet keys %s <key>" % (name, name),
                         screen=scr.tail(12))
    box = scr.prompt_box()
    if box is None:
        raise FleetError("no claude prompt box on '%s' (screen above); not typing" % name, screen=scr.tail(12))
    if box.typed and not force:
        raise FleetError("prompt box already holds: '%s' (someone typed there). "
                         "Re-run with --force to append anyway." % box.typed)
    # A grey suggested prompt (box.ghost) is not text anyone typed: the first typed key replaces it.
    term.type(text)
    deadline = ctx.monotonic() + TYPED_TIMEOUT
    while True:
        ctx.sleep(0.3)
        scr = term.screen()
        if S.shows_typed(scr, text):
            break
        if ctx.monotonic() >= deadline:
            raise FleetError("typed text not visible in the prompt box; not submitting (screen above)",
                             screen=scr.tail(8))
    if ctx.lifecycle.is_tracked(pane):
        # before the key press, so the prompt's UserPromptSubmit and Stop are ordered after it
        ctx.lifecycle.prompt_sent(pane)
    term.submit(now=now)
    ctx.say("sent%s to %s%s: %s" % (" (now)" if now else "", name, form_note(form), text))
    return 0


SLASH_COMMAND = re.compile(r"/[A-Za-z0-9_:-]+(\s|$)")


def slash_command(content):
    """(first line, rest) when the prompt's first line is a slash command, else None. Claude Code runs a
    slash command only when it is the first thing typed. Leading and trailing blank lines don't count;
    `rest` holds the lines after the first ("" if none), without blank lines around them."""
    lines = content.strip().splitlines()
    if not lines or not SLASH_COMMAND.match(lines[0].strip()):
        return None
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    while rest and not rest[-1].strip():
        rest.pop()
    return lines[0].strip(), "".join(line + "\n" for line in rest)


def read_prompt_file(path):
    return "Read %s and do what it says." % path


def read_stdin(ctx):
    """The prompt piped in for --prompt-stdin."""
    if ctx.stdin.isatty():
        raise FleetError("--prompt-stdin: stdin is a terminal; pipe the prompt in "
                         "(e.g. fleet ... --prompt-stdin < prompt.md)")
    text = ctx.stdin.read()
    if not text.strip():
        raise FleetError("--prompt-stdin: stdin is empty; nothing to send")
    return text


def save_prompt(ctx, text):
    """Save `text` to a new file under the state dir and return its path. Workers are told to read
    the file instead of having the text typed in: typing is slow for long prompts and flattens
    newlines. The file is kept, since a worker reads it after fleet has exited."""
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


SLASH_FORM = "slash command"


def typed_prompt(ctx, content, path=""):
    """(text to type, form) for a prompt given by file or stdin. `path`: the file `content` came
    from (--prompt-file), else "" and it is saved under the state dir if a file is needed.
    A slash command on the first line is typed as-is so Claude Code runs it (form SLASH_FORM); any
    lines after it go to a new file the command is told to read. Anything else: "Read <file> ..." (form "")."""
    cmd = slash_command(content)
    if cmd is None:
        return read_prompt_file(path or save_prompt(ctx, content)), ""
    first, rest = cmd
    if not rest:
        return first, SLASH_FORM
    return "%s (also read %s and follow it)" % (first, save_prompt(ctx, rest)), SLASH_FORM


def form_note(form):
    """The form for an output line: " (slash command)", or "" for text typed as given."""
    return " (%s)" % form if form else ""


def prompt_file_content(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def prompt_text(ctx, o):
    """(text to type, form) for a parsed new/send/broadcast: the inline text itself, or what
    typed_prompt makes of --prompt-file or --prompt-stdin. None for `new` without a prompt."""
    if o["prompt_file"]:
        path = prompt_path(o["prompt_file"])
        return typed_prompt(ctx, prompt_file_content(path), path)
    if o["prompt_stdin"]:
        return typed_prompt(ctx, read_stdin(ctx))
    if o.get("text"):
        return o["text"], ""
    return None


def cmd_send(ctx, argv):
    o = A.parse_send(argv)
    text, form = prompt_text(ctx, o)
    return send(ctx, roster(ctx).lookup(o["name"]), text, now=o["now"], force=o["force"], form=form)


WAIT_CODES = {"finished": 0, "exited": 0, "idle": 0, "interrupted": 5, "blocked": 4}


def wait(ctx, w, timeout):
    """0 when the turn after the last send finished (or the worker exited, or, never sent to, is
    idle), 5 when that turn was interrupted, 4 when blocked, 2 on timeout. `w`: a Roster handle."""
    [r] = wait_all(ctx, [w], timeout)
    if isinstance(r, FleetError):
        raise r
    code, line = r
    ctx.say(line)
    return code


def wait_all(ctx, handles, timeout):
    """Wait on every worker in `handles` (Roster handles) against one deadline, `timeout` seconds
    from now. Every round polls each worker still pending; a worker drops out once its wait is over
    (finished, exited, idle, interrupted or blocked). Returns, in the order given, (code, line) as `wait` would return and print it, or
    the FleetError that worker's wait failed with."""
    results = [None] * len(handles)
    pending = {}  # index -> (pane, name, look, recheck)
    for i, w in enumerate(handles):
        try:
            pane = R.require_worker(w).pane
        except FleetError as e:
            results[i] = e
            continue
        pending[i] = (pane, w.name) + screen_classifier(ctx, pane)
    deadline = ctx.monotonic() + timeout
    while pending:
        views = {}
        for i, (pane, name, look, recheck) in list(pending.items()):
            try:
                r = _wait_step(ctx, pane, name, look, recheck)
            except FleetError as e:  # fails this worker's wait, not the others'
                r = e
            if isinstance(r, str):
                views[i] = r
            else:
                results[i] = r
                del pending[i]
        if pending and ctx.monotonic() >= deadline:
            for i in pending:
                results[i] = (2, "still %s after %ds" % (views[i], timeout))
            break
        if pending:
            ctx.sleep(1)
    return results


def _wait_step(ctx, pane, name, look, recheck):
    """One poll of one worker: its (code, line) once the wait is over, the FleetError if it can't be
    waited on, else the view it is still in."""
    view = ctx.lifecycle.wait_view(pane, look, recheck)
    if view is None:
        return FleetError("'%s' is no longer tracked (its state file is gone)" % name)
    if view == "blocked":
        rec = ctx.lifecycle.load(pane) or {}
        return 4, "blocked: %s. See it with: fleet read %s" % (rec.get("detail", ""), name)
    if view in WAIT_CODES:
        return WAIT_CODES[view], view
    return view


def cmd_wait(ctx, argv):
    o = A.parse_wait(argv)
    return wait(ctx, roster(ctx).worker(o["name"]), o["timeout"])


class Watched:
    """One worker under `fleet watch`: what has been reported about it so far, so every turn end,
    change to blocked and exit is printed once. Starts from the record as it stands, corrected from
    the screen first, so a change from before the start (an interrupt nobody reconciled yet) is not
    reported as news."""

    def __init__(self, ctx, h):
        self.name, self.pane = h.name, h.pane
        self.look, self.recheck = screen_classifier(ctx, h.pane)
        rec = ctx.lifecycle.reconcile(h.pane, self.look, self.recheck) or {}
        self.ended = rec.get("ended_seq") or 0
        self.blocked_since = rec.get("state_since") if rec.get("state") == "blocked" else None
        self.exited = rec.get("state") == "exited"
        self.view = ""

    def poll(self, ctx):
        """The lines to print for what changed since the last poll; None once untracked."""
        view = ctx.lifecycle.wait_view(self.pane, self.look, self.recheck)
        rec = ctx.lifecycle.load(self.pane) if view is not None else None
        if rec is None:
            return None
        self.view = view
        lines = []
        ended = rec.get("ended_seq") or 0
        if ended > self.ended:  # one line even if several turns ended since the last poll
            self.ended = ended
            if rec.get("outcome") == "interrupted":
                lines.append("%s interrupted" % self.name)
            else:
                head = headline(rp.last_finished(rec))
                lines.append("%s finished%s" % (self.name, ": " + head if head else ""))
        if view == "blocked":
            if self.blocked_since != rec.get("state_since"):
                self.blocked_since = rec.get("state_since")
                lines.append("%s blocked: %s. See it with: fleet read %s"
                             % (self.name, rec.get("detail", ""), self.name))
        else:
            self.blocked_since = None
        if view == "exited" and not self.exited:
            lines.append("%s exited" % self.name)
        self.exited = view == "exited"
        return lines

    def settled(self):
        return lc.is_settled(self.view)


def headline(text):
    """The first non-blank line of a reply."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def cmd_watch(ctx, argv):
    """Print one line per turn end, change to blocked, exit or untracking of the watched workers, as
    it happens; 0 once all are settled at the same time, 2 at the timeout."""
    o = A.parse_watch(argv)
    if not o["to"]:
        ctx.store.ensure()
        sweep(ctx)
    ros = roster(ctx)
    handles = [ros.worker(n) for n in o["to"]] or ros.workers()
    if not handles:
        raise FleetError("no workers to watch")
    watched = [Watched(ctx, h) for h in handles]
    deadline = ctx.monotonic() + o["timeout"]
    while True:
        for w in list(watched):
            try:
                lines = w.poll(ctx)
            except FleetError as e:  # e.g. a lock held too long: try again next round
                ctx.warn("fleet: %s: %s" % (w.name, e))
                continue
            if lines is None:
                ctx.say("%s gone" % w.name)
                watched.remove(w)
            else:
                ctx.say(*lines)
        if all(w.settled() for w in watched):
            ctx.say("watch: all settled")
            return 0
        if ctx.monotonic() >= deadline:
            ctx.say("watch: timeout after %ds, still busy: %s"
                    % (o["timeout"], ", ".join(w.name for w in watched if not w.settled())))
            return 2
        ctx.sleep(1)


def cmd_broadcast(ctx, argv):
    o = A.parse_broadcast(argv)
    if not o["to"]:
        ctx.store.ensure()
        sweep(ctx)
    ros = roster(ctx)  # one for every recipient: the session is discovered once
    # (name to report, what to look up): every worker by pane id, so a renamed window still resolves
    targets = [(n, n) for n in o["to"]] or [(h.name, h.pane) for h in ros.workers()]
    if not targets:
        raise FleetError("no workers to send to")
    text, form = prompt_text(ctx, o)  # decided once: every recipient is typed the same text
    sent, rc = [], 0
    for name, query in targets:
        try:
            h = ros.lookup(query)
            send(ctx.quiet(), h, text, now=o["now"], force=o["force"], form=form)
        except FleetError as e:
            report_error(ctx, e)
            ctx.say("NOT sent to %s (reason above)" % name)
            rc = 1
        else:
            sent.append((name, h))
            ctx.say("sent to %s%s" % (name, form_note(form)))
    if o["wait"] is None:
        return rc
    # One deadline for all: the wait takes about --wait seconds however many workers stay busy.
    # Reported in send order, whatever order they finish in.
    blocked = interrupted = timed_out = False
    for (w, h), r in zip(sent, wait_all(ctx, [h for _, h in sent], o["wait"])):
        if isinstance(r, FleetError):
            report_error(ctx, r)
            wr, line = 1, ""
        else:
            wr, line = r
        if wr == 0:
            ctx.say("##### %s: %s" % (w, line))  # finished, exited or idle
            last(ctx, h, 1)
        elif wr == 4:
            ctx.say("##### %s: BLOCKED, see it with: fleet read %s" % (w, w))
            blocked = True
        elif wr == 5:
            ctx.say("##### %s: INTERRUPTED, see it with: fleet read %s" % (w, w))
            interrupted = True
        elif wr == 2:
            ctx.say("##### %s: still busy after %ss" % (w, o["wait"]))
            timed_out = True
        else:
            ctx.say("##### %s: wait failed (%s)" % (w, wr))
            rc = 1
    return broadcast_code(rc != 0, blocked, timed_out, interrupted)


def broadcast_code(failed, blocked, timed_out, interrupted=False):
    """1 if any send or wait failed, else 4 if any worker blocked, else 5 if any turn was
    interrupted, else 2 if any timed out."""
    if failed:
        return 1
    if blocked:
        return 4
    if interrupted:
        return 5
    if timed_out:
        return 2
    return 0


def cmd_read(ctx, argv):
    o = A.parse_read(argv)
    pane = roster(ctx).lookup(o["name"]).pane
    text = ctx.terminal(pane).scrollback(o["n"])
    for line in text.split("\n"):
        if line:
            ctx.say(line)
    return 0


INTERRUPTED_SLOT = "(interrupted: no reply)"


def last(ctx, w, n):
    """The replies of worker `w`'s last n turns (a Roster handle), oldest first; an interrupted turn
    prints INTERRUPTED_SLOT in its place."""
    rec = ctx.lifecycle.load(R.require_worker(w).pane)
    if rec is None:
        raise FleetError("'%s' is no longer tracked (its state file is gone)" % w.name)
    picked = rp.recent(rec, n, ctx.sleep, ctx.monotonic)
    if not picked:
        ctx.say("(no reply yet)")
        return 0
    for r in picked:
        ctx.say(INTERRUPTED_SLOT if r is None else r, "----")
    return 0


def cmd_last(ctx, argv):
    o = A.parse_last(argv)
    return last(ctx, roster(ctx).worker(o["name"]), o["n"])


def cmd_keys(ctx, argv):
    o = A.parse_keys(argv)
    pane = roster(ctx).lookup(o["name"]).pane
    ctx.terminal(pane).press(*o["keys"])
    ctx.lifecycle.dialog_answered(pane)
    return 0


def age(seconds):
    d = max(0, int(seconds))
    if d < 60:
        return "%ds" % d
    if d < 3600:
        return "%dm" % (d // 60)
    return "%dh" % (d // 3600)


def cmd_status(ctx, argv):
    o = A.parse_status(argv)
    ctx.store.ensure()
    sweep(ctx)
    ros = roster(ctx)
    s = ros.session
    recs = [r for r in (reconcile(ctx, w.pane) for w in ros.workers()) if r is not None]
    untracked = [h.name for h in ros.untracked()]
    if o["json"]:
        workers = [dict(r, reply=rp.latest(r)) for r in recs]
        ctx.say(json.dumps({"workers": workers, "untracked": untracked}, indent=2, ensure_ascii=False))
        return 0
    ctx.say("fleet session: %s" % s)
    if not recs and not untracked:
        ctx.say("(no workers)")
        return 0
    row = "%-24s %-8s %-11s %-5s %-22s %s"
    ctx.say(row % ("WORKER", "STATE", "OUTCOME", "AGE", "DETAIL", "REPLY"))
    now = ctx.now()
    for r in recs:
        msg = re.sub(r"\s+", " ", rp.latest(r)).strip()
        outcome = r.get("outcome") or "none"
        ctx.say(row % (r.get("name", ""), r.get("state", ""), "" if outcome == "none" else outcome,
                       age(now - (r.get("state_since") or 0)),
                       (r.get("detail") or "")[:22], msg[:80]))
    for u in untracked:
        ctx.say("%-24s %-8s" % (u, "untracked"))
    return 0


def cmd_rm(ctx, argv):
    o = A.parse_rm(argv)
    name = o["name"]
    pane = roster(ctx).lookup(name).pane
    rec = ctx.lifecycle.load(pane) or {}
    wt, repo = rec.get("worktree") or "", rec.get("repo") or ""
    if wt and not o["force"]:
        ctx.worktrees.check_removable(wt)
    ctx.tmux.kill_window(pane)
    ctx.lifecycle.forget(pane)
    ctx.say("killed window '%s'" % name)
    if wt:
        lines = ctx.worktrees.remove(repo, wt, force=o["force"], branch=o["branch"])
        if lines is not None:
            ctx.say("removed worktree %s" % wt, *lines)
    return 0


COMMANDS = {
    "up": cmd_up, "new": cmd_new, "status": cmd_status, "send": cmd_send, "wait": cmd_wait,
    "read": cmd_read, "last": cmd_last, "keys": cmd_keys, "rm": cmd_rm, "broadcast": cmd_broadcast,
    "watch": cmd_watch,
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
        report_error(ctx, e)
        return e.code


def report_error(ctx, e):
    """Print a FleetError: the screen it is about to stdout, then `fleet: <message>` to stderr."""
    ctx.say(*e.screen)
    ctx.warn("fleet: %s" % e)


def main(argv, fleet_path):
    here = os.path.dirname(fleet_path)
    ctx = Ctx(fleet_path=fleet_path, hook_path=os.path.join(here, "fleet-hook"))
    try:
        return run(ctx, argv)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 1
