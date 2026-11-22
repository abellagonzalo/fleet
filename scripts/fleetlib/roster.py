"""The Roster: the only place that decides which pane of the fleet session a name refers to, and what
kind of pane each one is (orchestrator, worker, untracked pane, other). Vocabulary: GLOSSARY.md.

Pure: built once per command from the session's pane list (the dicts `Tmux.panes()` returns), the
set of pane ids the worker lifecycle tracks, and the caller's own pane id. It reads no records and
asks tmux nothing; the CLI builds it (see cli.roster) and loads records through the Lifecycle.
"""

import re
from collections import namedtuple

from .errors import FleetError

ORCHESTRATOR = "orchestrator"
WORKER = "worker"
UNTRACKED = "untracked"
OTHER = "other"

Handle = namedtuple("Handle", "pane name kind")
Handle.__doc__ = "A resolved pane: its id, its worker (or window) name, and its kind."


class Roster:
    def __init__(self, session, panes, tracked, own_pane):
        self.session = session
        self.panes = [p for p in panes if p.get("pane_id")]
        self.tracked = set(tracked)
        self.own_pane = own_pane or ""

    def _handle(self, p):
        return Handle(p["pane_id"], p.get("@fleet_name") or p["window_name"], self._kind(p))

    def _kind(self, p):
        if p.get("@fleet_role") == "orch":
            return ORCHESTRATOR
        if p["pane_id"] in self.tracked:
            return WORKER
        if is_claude_command(p.get("pane_current_command") or ""):
            return UNTRACKED
        return OTHER

    def lookup(self, name):
        """The pane a worker name (a window name of the session) or raw pane id ("%12", only a pane
        of the session) refers to. Never the caller's own pane or an orchestrator's."""
        if not name:
            raise FleetError("no worker name given")
        if name == "orch":
            raise FleetError("'orch' is the orchestrator's pane, not a worker")
        match = [p for p in self.panes if p["window_name"] == name]
        if not match and re.match(r"^%\d+$", name):
            match = [p for p in self.panes if p["pane_id"] == name]
        if not match:
            raise FleetError("no worker '%s' in fleet session '%s'" % (name, self.session))
        if len(match) > 1:  # a split window: the pane that carries the name
            marked = [p for p in match if p.get("@fleet_name") == name]
            if not marked:
                raise FleetError("window '%s' has %d panes and none is the worker" % (name, len(match)))
            match = marked
        h = self._handle(match[0])
        if h.pane == self.own_pane:
            raise FleetError("'%s' resolves to this pane (the orchestrator), not a worker" % name)
        if h.kind == ORCHESTRATOR:
            raise FleetError("'%s' is the orchestrator's pane, not a worker" % name)
        return h

    def worker(self, name):
        """lookup(), for commands that need a worker's record."""
        return require_worker(self.lookup(name))

    def workers(self):
        return [h for h in map(self._handle, self.panes) if h.kind == WORKER]

    def untracked(self):
        return [h for h in map(self._handle, self.panes) if h.kind == UNTRACKED]

    def check_new_name(self, name):
        """Refuse a name `fleet new` cannot give a worker."""
        if "/" in name:
            raise FleetError("'%s' contains '/': worktrees live at <repo>/<branch>, so names must be flat" % name)
        if name == "orch":
            raise FleetError("'orch' is the orchestrator's window")
        if re.match(r"^%\d+$", name) or name.startswith("-"):
            raise FleetError("'%s' cannot be a worker name" % name)
        if any(p["window_name"] == name for p in self.panes):
            raise FleetError("a window named '%s' already runs in '%s'" % (name, self.session))


def require_worker(h):
    """`h` itself, if it is a worker."""
    if h.kind != WORKER:
        raise FleetError("'%s' is not a tracked worker (no hook state)" % h.name)
    return h


def is_fleet_session(name):
    return name == "fleet" or name.startswith("fleet-")


def find_session(environ, session_of, sessions):
    """The fleet session a command acts on: $FLEET_SESSION, else the caller's own session if it is a
    fleet session, else the only fleet session running. session_of(pane) and sessions() ask tmux;
    they are called only when needed."""
    s = environ.get("FLEET_SESSION", "")
    if s:
        return s
    pane = environ.get("TMUX_PANE", "")
    if environ.get("TMUX") and pane:
        try:
            cur = session_of(pane)
        except FleetError:
            cur = ""
        if is_fleet_session(cur):
            return cur
    found = [x for x in sessions() if is_fleet_session(x)]
    if not found:
        raise FleetError("no fleet session running; start one with: fleet up [name]")
    if len(found) > 1:
        raise FleetError("several fleet sessions running (%s); set FLEET_SESSION=<session>" % " ".join(found))
    return found[0]


def is_claude_command(cmd):
    """Does a pane's current command look like Claude Code (it shows as its version, e.g. 2.1.329)?"""
    return "claude" in cmd or re.match(r"^\d+\.\d+\.\d+$", cmd) is not None
