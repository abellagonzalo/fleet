"""The thin layer over tmux, git and wt. Everything that runs a process goes through here, so the tests
can swap in fakes (tests/fakes.py) with the same methods.

Safety rule enforced here, not by callers: tmux is never given an empty target (`-t ""` means the
current pane), and keys are only ever sent to an explicit pane id ("%12") that is not the pane this
command runs in.
"""

import os
import subprocess

from .errors import FleetError


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
    """tmux, one method per operation fleet needs."""

    def __init__(self, own_pane=None):
        # The pane this command runs in (the orchestrator, when it is the caller). Never a target.
        self.own_pane = own_pane if own_pane is not None else os.environ.get("TMUX_PANE", "")
        self._checked = set()  # panes verified not to be an orchestrator

    def _tmux(self, *args, check=True, timeout=None):
        return _run(["tmux"] + list(args), check=check, timeout=timeout)

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

    def capture(self, pane, ansi=False, scrollback=0):
        args = ["capture-pane", "-p", "-t", _target(pane)]
        if ansi:
            args.append("-e")
        if scrollback:
            args += ["-S", "-%d" % scrollback]
        return self._tmux(*args).stdout

    # ---- changes ----

    def pane_role(self, pane):
        """The pane's @fleet_role ("orch" for an orchestrator, else "")."""
        return self._tmux("display", "-p", "-t", _target(pane), "#{@fleet_role}").stdout.strip()

    def _pane_for_keys(self, pane, orch_ok=False):
        _target(pane)
        if not pane.startswith("%"):
            raise FleetError("internal: keys go to pane ids only, not %r" % pane)
        if self.own_pane and pane == self.own_pane:
            raise FleetError("internal: refusing to send keys to this pane (%s), the caller's own" % pane)
        if not orch_ok:
            if pane not in self._checked:
                if self.pane_role(pane) == "orch":
                    raise FleetError("internal: refusing to send keys to %s, an orchestrator's pane" % pane)
                self._checked.add(pane)
        return pane

    def send_keys(self, pane, *keys, orch_ok=False):
        """tmux key names (Enter, Escape, Down, C-u...). orch_ok only for `fleet up` starting one."""
        self._tmux("send-keys", "-t", self._pane_for_keys(pane, orch_ok), *keys)

    def send_literal(self, pane, text, orch_ok=False):
        self._tmux("send-keys", "-t", self._pane_for_keys(pane, orch_ok), "-l", text)

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
    """worktrunk. Hook and remove output goes to our stderr, as progress for the user."""

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

    def hook(self, worktree, name):
        import sys
        p = _run(["wt", "-C", worktree, "hook", name, "--yes", "--foreground"], check=False,
                 stdout=sys.stderr, stderr=None)
        return p.returncode == 0

    def remove(self, repo, worktree, force=False):
        import sys
        argv = ["wt", "-C", repo, "remove", worktree, "--no-delete-branch", "--foreground", "--yes"]
        if force:
            argv.append("--force")
        return _run(argv, check=False, stdout=sys.stderr, stderr=None).returncode == 0


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
