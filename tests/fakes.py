"""Fakes for the system layer, plus helpers to build a Ctx around them."""

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

from fleetlib import cli  # noqa: E402
from fleetlib.errors import FleetError  # noqa: E402
from fleetlib.system import Tmux  # noqa: E402

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
    def __init__(self, pane_id, window, session, fleet_name="", role="", command="2.1.329"):
        self.id = pane_id
        self.window = window
        self.session = session
        self.fleet_name = fleet_name
        self.role = role
        self.command = command
        self.typed = ""  # what has been typed into a live prompt box
        self.screen = None  # fixed screen (raw ansi), or None for a live prompt box
        self.screens = []  # queued screens returned one per capture before `screen`
        self.submitted = []  # (text, how) for every Enter / Ctrl+Enter
        self.ghost = ""
        self.echo = True  # does typing show up in the prompt box?

    def render(self):
        if self.screens:
            return self.screens.pop(0)
        if self.screen is not None:
            return self.screen
        if self.typed:
            return box_screen(typed=self.typed if self.echo else "")
        return box_screen(ghost=self.ghost)


class FakeTmux(Tmux):
    """Real key-target guards (inherited), fake everything that would run tmux."""

    def __init__(self, own_pane="%0"):
        super().__init__(own_pane=own_pane)
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

    def capture(self, pane, ansi=False, scrollback=0):
        if not pane:
            raise AssertionError("capture with an empty target")
        self.calls.append(("capture", pane))
        if pane not in self.panes_:
            raise FleetError("can't find pane %s" % pane)
        raw = self.panes_[pane].render()
        if not ansi:
            import re
            raw = re.sub(r"\x1b\[[0-9;:]*m", "", raw)
        return raw

    # ---- changes ----
    def _tmux(self, *args, check=True):
        self.calls.append(args)
        if args[0] == "send-keys":
            pane = args[2]
            p = self.panes_[pane]
            if "-l" in args:
                text = args[args.index("-l") + 1]
                if text == cli.CTRL_ENTER:
                    p.submitted.append((p.typed, "now"))
                    p.typed = ""
                else:
                    p.typed += text
            else:
                for k in args[3:]:
                    if k == "Enter":
                        p.submitted.append((p.typed, "enter"))
                        p.typed = ""
        return None

    def set_window_option(self, target, name, value, check=True):
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

    def keys_sent_to(self, pane):
        return [c for c in self.calls if c and c[0] == "send-keys" and c[2] == pane]


class FakeGit:
    def __init__(self):
        self.responses = {}  # tuple(args) -> (rc, out)
        self.calls = []

    def run(self, repo, *args, check=False):
        self.calls.append((repo,) + args)
        return self.responses.get(args, (0, ""))

    def ok(self, repo, *args):
        return self.run(repo, *args)[0] == 0

    def out(self, repo, *args):
        rc, out = self.run(repo, *args)
        return out.strip() if rc == 0 else ""


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


def make_ctx(tmux=None, state_dir=None, environ=None, clock=None, git=None, wt=None):
    env = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%0"}
    if state_dir:
        env["FLEET_STATE_DIR"] = state_dir
    env.update(environ or {})
    clock = clock or Clock()
    ctx = cli.Ctx(tmux=tmux or FakeTmux(own_pane=env.get("TMUX_PANE", "")), git=git or FakeGit(),
                  wt=wt or FakeWt(), environ=env, out=io.StringIO(), err=io.StringIO(),
                  now=clock.now, sleep=clock.sleep, monotonic=clock.monotonic,
                  fleet_path="/opt/fleet/scripts/fleet", hook_path="/opt/fleet/scripts/fleet-hook",
                  isatty=lambda: False, cwd=lambda: "/work")
    ctx.delay = 0
    ctx.clock = clock
    return ctx
