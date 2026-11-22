"""The thin layer over tmux, git and wt. Everything that runs a process goes through here, so the tests
can swap in fakes (tests/fakes.py), or swap out the process runner to test this layer itself.

Safety rule enforced here, not by callers: tmux is never given an empty target (`-t ""` means the
current pane), and keys are only ever sent, through a Terminal, to an explicit pane id ("%12") that is
neither the pane this command runs in nor an orchestrator's (unless opened for one, by `fleet up`).
"""

import os
import subprocess
import time

from .errors import FleetError
from .screen import Screen

DELAY_DEFAULT = 0.02  # seconds between typed characters, unless FLEET_DELAY says otherwise
CTRL_ENTER = "\x1b[13;5u"  # Ctrl+Enter as CSI-u: interrupt the turn and run the prompt now


def _run(argv, cwd=None, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=None,
         timeout=None):
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=stdout, stderr=stderr, input=input,
                           universal_newlines=True, timeout=timeout)
    except FileNotFoundError:
        raise FleetError("%s not found" % argv[0])
    except subprocess.TimeoutExpired:
        raise FleetError("%s timed out after %ss" % (" ".join(argv[:3]), timeout))
    if check and p.returncode != 0:
        err = (p.stderr or "").strip() if stderr == subprocess.PIPE else ""
        raise FleetError("%s failed (exit %d)%s" % (" ".join(argv[:3]), p.returncode,
                                                     ": " + err if err else ""))
    return p


def _target(t):
    if not t:
        raise FleetError("internal: empty tmux target; refusing (it would mean the current pane)")
    return t


PANE_FIELDS = ("window_name", "pane_id", "pane_current_command", "@fleet_name", "@fleet_role")


class Tmux:
    """tmux's sessions, windows and options, one method per operation fleet needs. A pane's keyboard
    and screen are a Terminal: `terminal(pane)`."""

    def __init__(self, own_pane=None, run=None):
        # The pane this command runs in (the orchestrator, when it is the caller). Never a target.
        self.own_pane = own_pane if own_pane is not None else os.environ.get("TMUX_PANE", "")
        self._run = run or _run

    def _tmux(self, *args, check=True, timeout=None):
        return self._run(["tmux"] + list(args), check=check, timeout=timeout)

    def terminal(self, pane, orchestrator_ok=False, delay=DELAY_DEFAULT, sleep=time.sleep):
        """The keyboard and screen of `pane`. orchestrator_ok only for `fleet up` starting one."""
        return TmuxTerminal(self, pane, orchestrator_ok, delay, sleep)

    # ---- queries ----

    def sessions(self):
        p = self._tmux("list-sessions", "-F", "#S", check=False)
        return [s for s in p.stdout.splitlines() if s] if p.returncode == 0 else []

    def has_session(self, name):
        return self._tmux("has-session", "-t", "=" + _target(name), check=False).returncode == 0

    def session_of(self, pane):
        return self._tmux("display", "-p", "-t", _target(pane), "#S").stdout.strip()

    def panes(self, session):
        """Every pane of a session, as dicts keyed by PANE_FIELDS."""
        fmt = "\t".join("#{%s}" % f for f in PANE_FIELDS)
        out = self._tmux("list-panes", "-s", "-t", "=" + _target(session), "-F", fmt).stdout
        return [dict(zip(PANE_FIELDS, line.split("\t"))) for line in out.splitlines() if line]

    def all_panes(self):
        """{pane_id: @fleet_name} for every pane on the server, or None when tmux cannot be asked
        (callers must then not conclude that panes are gone)."""
        p = self._tmux("list-panes", "-a", "-F", "#{pane_id}\t#{@fleet_name}", check=False)
        if p.returncode != 0:
            return None
        res = {}
        for line in p.stdout.splitlines():
            pid, _, name = line.partition("\t")
            if pid:
                res[pid] = name
        return res

    def pane_role(self, pane):
        """The pane's @fleet_role ("orch" for an orchestrator, else "")."""
        return self._tmux("display", "-p", "-t", _target(pane), "#{@fleet_role}").stdout.strip()

    # ---- changes ----

    def set_window_option(self, target, name, value, check=True, timeout=None):
        self._tmux("set", "-w", "-t", _target(target), name, value, check=check, timeout=timeout)

    def set_pane_option(self, target, name, value):
        self._tmux("set", "-p", "-t", _target(target), name, value)

    def new_session(self, name, window, cwd, width=200, height=50):
        """Returns the pane id of the session's first window."""
        return self._tmux("new-session", "-d", "-s", _target(name), "-n", window, "-c", cwd,
                          "-x", str(width), "-y", str(height), "-P", "-F", "#{pane_id}").stdout.strip()

    def new_window(self, session, name, cwd):
        """Returns the new window's pane id."""
        return self._tmux("new-window", "-d", "-t", "=%s:" % _target(session), "-n", name, "-c", cwd,
                          "-P", "-F", "#{pane_id}").stdout.strip()

    def kill_window(self, pane):
        self._tmux("kill-window", "-t", _target(pane))

    def attach(self, session):
        """Replace this process with `tmux attach` (only when run from a terminal)."""
        os.execvp("tmux", ["tmux", "attach", "-t", "=" + _target(session)])


class TmuxTerminal:
    """The Terminal port over tmux: one pane's keyboard (type, submit, press) and screen (screen,
    scrollback). Owns how keys are encoded and paced, and the pane guards."""

    def __init__(self, tmux, pane, orchestrator_ok=False, delay=DELAY_DEFAULT, sleep=time.sleep):
        self._server = tmux
        self.pane = _target(pane)
        self._orchestrator_ok = orchestrator_ok
        self._delay = delay
        self._sleep = sleep
        self._checked = False  # keys allowed: the guards have passed once

    def _keys(self, *args):
        self._guard()
        self._server._tmux("send-keys", "-t", self.pane, *args)

    def _guard(self):
        if self._checked:
            return
        pane = self.pane
        if not pane.startswith("%"):
            raise FleetError("internal: keys go to pane ids only, not %r" % pane)
        if self._server.own_pane and pane == self._server.own_pane:
            raise FleetError("internal: refusing to send keys to this pane (%s), the caller's own" % pane)
        if not self._orchestrator_ok and self._server.pane_role(pane) == "orch":
            raise FleetError("internal: refusing to send keys to %s, an orchestrator's pane" % pane)
        self._checked = True

    def type(self, text, paced=True):
        """Type `text` literally: a key at a time at the human pace a Claude Code prompt box needs,
        or, paced=False, in one go (a shell command line)."""
        if not paced:
            self._keys("-l", text)
            return
        for ch in text:
            self._keys("-l", ch)
            self._sleep(self._delay)

    def submit(self, now=False):
        """Enter; now=True: Ctrl+Enter, which interrupts a running turn and runs the prompt now."""
        if now:
            self._keys("-l", CTRL_ENTER)
        else:
            self._keys("Enter")

    def press(self, *keys):
        """tmux key names: Enter, Escape, Down, C-u..."""
        self._keys(*keys)

    def screen(self):
        """What the pane shows now, with attributes (a grey suggestion is not typed text)."""
        return Screen(self._capture("-e"), ansi=True)

    def scrollback(self, n):
        """The pane as plain text, with `n` lines of history above the screen."""
        return self._capture("-S", "-%d" % n) if n else self._capture()

    def _capture(self, *args):
        return self._server._tmux("capture-pane", "-p", "-t", self.pane, *args).stdout


class Git:
    def run(self, repo, *args, check=False):
        """(returncode, stdout) of `git -C repo args`."""
        p = _run(["git", "-C", repo] + list(args), check=check)
        return p.returncode, p.stdout

    def ok(self, repo, *args):
        return self.run(repo, *args)[0] == 0

    def out(self, repo, *args):
        rc, out = self.run(repo, *args)
        return out.strip() if rc == 0 else ""


class Wt:
    """worktrunk. Hook and remove progress goes to our stderr, for the user."""

    def available(self):
        from shutil import which
        return which("wt") is not None

    def list_json(self, repo):
        p = _run(["wt", "-C", repo, "list", "--format", "json"], check=False)
        return p.stdout if p.returncode == 0 else ""

    def switch(self, repo, branch, create=False, base=None):
        """Returns (returncode, stdout)."""
        argv = ["wt", "-C", repo, "switch"]
        if create:
            argv += ["--create", branch, "--base", base]
        else:
            argv.append(branch)
        argv += ["--no-cd", "--no-hooks", "--yes", "--format", "json"]
        p = _run(argv, check=False, stderr=None)
        return p.returncode, p.stdout

    def hook(self, worktree, name, out=None):
        """Run the `name` hooks of `worktree`; their output goes to our stderr, or line by line to
        `out` when given. Returns whether they succeeded."""
        import sys
        argv = ["wt", "-C", worktree, "hook", name, "--yes", "--foreground"]
        if out is None:
            return _run(argv, check=False, stdout=sys.stderr, stderr=None).returncode == 0
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, universal_newlines=True, errors="replace")
        except FileNotFoundError:
            raise FleetError("wt not found")
        with p:
            for line in p.stdout:
                out(line.rstrip("\n"))
        return p.returncode == 0

    def remove(self, repo, worktree, force=False, branch="merged"):
        """Remove `worktree`; `force` discards its changes. `branch`: "merged" (worktrunk deletes it if
        merged), "keep" or "delete" (delete even unmerged). Returns (ok, the worktree's entry of
        `wt remove --format json`: branch, branch_outcome, branch_checked_out_at; {} if unreadable)."""
        import json
        argv = ["wt", "-C", repo, "remove", worktree, "--foreground", "--yes", "--format", "json"]
        if branch == "keep":
            argv.append("--no-delete-branch")
        elif branch == "delete":
            argv.append("--force-delete")
        if force:
            argv.append("--force")
        p = _run(argv, check=False, stdout=subprocess.PIPE, stderr=None)
        try:
            out = json.loads(p.stdout or "null")
        except ValueError:
            out = None
        if isinstance(out, list):
            out = next((e for e in out if isinstance(e, dict) and e.get("kind") == "worktree"), None)
        return p.returncode == 0, out if isinstance(out, dict) else {}


def notify(title, message):
    """macOS notification, fire and forget."""
    try:
        subprocess.Popen(
            ["osascript", "-e", "on run argv", "-e",
             "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run",
             message, title],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        pass
