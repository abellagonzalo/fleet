"""Fakes for the system layer, plus helpers to build a Ctx around them."""

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from fleetlib import cli  # noqa: E402
from fleetlib.errors import FleetError  # noqa: E402
from fleetlib.screen import Screen  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


RULE = "\x1b[38;5;244m" + "─" * 60 + "\n"


def box_screen(typed="", ghost="", footer="  \x1b[38;5;246m⏸ manual mode on\x1b[39m", above=""):
    """A minimal Claude screen in `capture-pane -e` form: [above] rule, prompt row, rule, footer."""
    row = "\x1b[39m❯ " + typed
    if ghost:
        row += "\x1b[2m" + ghost + "\x1b[0m"
    return above + RULE + row + "\n" + RULE + "\x1b[39m" + footer + "\n"


class FakePane:
    """A pane: what tmux knows about it, plus what its terminal holds (see FakeTerminal)."""

    def __init__(self, pane_id, window, session, fleet_name="", role="", command="2.1.329"):
        self.id = pane_id
        self.window = window
        self.session = session
        self.fleet_name = fleet_name
        self.role = role
        self.command = command
        self.typed = ""  # what has been typed into a live prompt box
        self.shown = None  # fixed screen (raw ansi), or None for a live prompt box
        self.screens = []  # queued screens returned one per screen read before `shown`
        self.intents = []  # ("type", text, paced) | ("submit", now) | ("press", keys), in order
        self.submitted = []  # (what the box held, "enter" | "now") at every submit
        self.opened = []  # orchestrator_ok of every Terminal opened on this pane
        self.ghost = ""
        self.echo = True  # does typing show up in the prompt box?
        self.on_submit = None  # callback() just before a submit lands
        self.on_screen = None  # callback() on every screen read

    def render(self):
        if self.on_screen:
            self.on_screen()
        if self.screens:
            return self.screens.pop(0)
        if self.shown is not None:
            return self.shown
        if self.typed:
            return box_screen(typed=self.typed if self.echo else "")
        return box_screen(ghost=self.ghost)


class FakeTerminal:
    """The Terminal port over a FakePane: records intents and serves the pane's screens. By default
    the screen is a prompt box holding what was typed, so send's visibility check passes.

    Keys to the caller's own pane, or to an orchestrator's not opened with orchestrator_ok, fail the
    test outright: the commands must refuse those before opening a Terminal."""

    def __init__(self, tmux, pane_id, orchestrator_ok):
        self.tmux = tmux
        self.pane_id = pane_id
        self.orchestrator_ok = orchestrator_ok
        if pane_id in tmux.panes_:
            tmux.panes_[pane_id].opened.append(orchestrator_ok)

    @property
    def p(self):
        """The pane, looked up on every use: like tmux, a gone pane fails when used, not when opened."""
        if self.pane_id not in self.tmux.panes_:
            raise FleetError("can't find pane %s" % self.pane_id)
        return self.tmux.panes_[self.pane_id]

    def _key(self, intent):
        p = self.p
        if p.id == self.tmux.own_pane or (p.role == "orch" and not self.orchestrator_ok):
            raise AssertionError("keys %r to %s, which fleet must never type into" % (intent, p.id))
        p.intents.append(intent)

    def type(self, text, paced=True):
        self._key(("type", text, paced))
        self.p.typed += text

    def submit(self, now=False):
        if self.p.on_submit:
            self.p.on_submit()
        self._key(("submit", now))
        self.p.submitted.append((self.p.typed, "now" if now else "enter"))
        self.p.typed = ""

    def press(self, *keys):
        self._key(("press", keys))

    def screen(self):
        return Screen(self.p.render(), ansi=True)

    def scrollback(self, n):
        return re.sub(r"\x1b\[[0-9;:]*m", "", self.p.render())


class FakeTmux:
    """Sessions, windows and options of a fake tmux server; terminal() opens a FakeTerminal."""

    def __init__(self, own_pane="%0"):
        self.own_pane = own_pane
        self.panes_ = {}
        self.calls = []
        self.window_options = {}
        self.pane_options = {}
        self.extra_sessions = []
        self.next_id = 50
        self.server_up = True

    # ---- setup ----
    def add(self, session, window, pane_id=None, **kw):
        """A pane; role="orch" marks an orchestrator."""
        if pane_id is None:
            pane_id = "%%%d" % self.next_id
            self.next_id += 1
        p = FakePane(pane_id, window, session, **kw)
        self.panes_[pane_id] = p
        return p

    def terminal(self, pane, orchestrator_ok=False, delay=0, sleep=None):
        if not pane:
            raise AssertionError("a terminal on an empty target")
        return FakeTerminal(self, pane, orchestrator_ok)

    def intents(self):
        """Every pane's intents, as (pane id, intent)."""
        return [(p.id, i) for p in self.panes_.values() for i in p.intents]

    # ---- queries ----
    def sessions(self):
        seen = []
        for p in self.panes_.values():
            if p.session not in seen:
                seen.append(p.session)
        return seen + [s for s in self.extra_sessions if s not in seen]

    def has_session(self, name):
        return name in self.sessions()

    def session_of(self, pane):
        if pane not in self.panes_:
            raise FleetError("can't find pane %s" % pane)
        return self.panes_[pane].session

    def panes(self, session):
        if session not in FakeTmux.sessions(self):  # not self.sessions: tests count the CLI's calls
            raise FleetError("can't find session %s" % session)
        return [{"window_name": p.window, "pane_id": p.id, "pane_current_command": p.command,
                 "@fleet_name": p.fleet_name, "@fleet_role": p.role}
                for p in self.panes_.values() if p.session == session]

    def all_panes(self):
        if not self.server_up:
            return None
        return {p.id: p.fleet_name for p in self.panes_.values()}

    def pane_role(self, pane):
        if pane not in self.panes_:
            raise FleetError("can't find pane %s" % pane)
        return self.panes_[pane].role

    # ---- changes ----
    def set_window_option(self, target, name, value, check=True, timeout=None):
        self.window_options[(target, name)] = value

    def set_pane_option(self, target, name, value):
        self.pane_options[(target, name)] = value
        if name == "@fleet_name" and target in self.panes_:
            self.panes_[target].fleet_name = value
        if name == "@fleet_role" and target in self.panes_:
            self.panes_[target].role = value

    def new_session(self, name, window, cwd, width=200, height=50):
        return self.add(name, window).id

    def new_window(self, session, name, cwd):
        return self.add(session, name).id

    def kill_window(self, pane):
        self.calls.append(("kill-window", pane))
        del self.panes_[pane]

    def attach(self, session):
        self.calls.append(("attach", session))


class FakeWt:
    def __init__(self):
        self.calls = []
        self.list_out = '{"items": []}'
        self.switch_result = (0, "{}")
        self.removed = []

    def available(self):
        return True

    def list_json(self, repo):
        return self.list_out

    def switch(self, repo, branch, create=False, base=None):
        self.calls.append(("switch", repo, branch, create, base))
        return self.switch_result

    def hook(self, worktree, name):
        self.calls.append(("hook", worktree, name))
        return True

    def remove(self, repo, worktree, force=False):
        self.removed.append((repo, worktree, force))
        return True


class FakeWorktrees:
    """Stub Worktrees for CLI tests: answers set by the test, calls recorded."""

    def __init__(self):
        self.calls = []
        self.ensured = None  # (dir, repo) that ensure returns
        self.errors = {}  # method name -> FleetError it raises
        self.removed = True  # what remove returns (False: the worktree was already gone)

    def _call(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.errors:
            raise self.errors[name]

    def ensure(self, src, branch, base=""):
        self._call("ensure", src, branch, base)
        return self.ensured

    def check_removable(self, worktree):
        self._call("check_removable", worktree)

    def remove(self, repo, worktree, force=False):
        self._call("remove", repo, worktree, force)
        return self.removed


class Clock:
    """Fake time: sleep advances it."""

    def __init__(self, t=1_000_000):
        self.t = float(t)
        self.on_sleep = None  # callback(t) after each sleep

    def now(self):
        return int(self.t)

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep(self.t)


def make_ctx(tmux=None, state_dir=None, environ=None, clock=None, worktrees=None):
    env = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%0"}
    if state_dir:
        env["FLEET_STATE_DIR"] = state_dir
    env.update(environ or {})
    clock = clock or Clock()
    ctx = cli.Ctx(tmux=tmux or FakeTmux(own_pane=env.get("TMUX_PANE", "")),
                  worktrees=worktrees or FakeWorktrees(), environ=env, out=io.StringIO(), err=io.StringIO(),
                  now=clock.now, sleep=clock.sleep, monotonic=clock.monotonic,
                  fleet_path="/opt/fleet/scripts/fleet", hook_path="/opt/fleet/scripts/fleet-hook",
                  isatty=lambda: False, cwd=lambda: "/work")
    ctx.clock = clock
    return ctx
