"""The Roster: which pane of the fleet session a name refers to, and what kind each pane is.
Table tests over plain pane lists: no tmux, no records."""

import re
import unittest

import fakes  # noqa: F401  (sets sys.path)

from fleetlib import roster as R
from fleetlib.errors import FleetError


def pane(pane_id, window, fleet_name="", role="", command="2.1.329"):
    return {"window_name": window, "pane_id": pane_id, "pane_current_command": command,
            "@fleet_name": fleet_name, "@fleet_role": role}


ORCH = pane("%0", "orch", role="orch")
ALPHA = pane("%1", "alpha", fleet_name="alpha")
BETA = pane("%2", "beta", fleet_name="beta")


def roster(*panes, tracked=("%1", "%2"), own="%0", session="fleet"):
    return R.Roster(session, list(panes), set(tracked), own)


class Lookup(unittest.TestCase):
    def test_resolves(self):
        ros = roster(ORCH, ALPHA, BETA)
        for name, want in (
            ("alpha", ("%1", "alpha", R.WORKER)),
            ("beta", ("%2", "beta", R.WORKER)),
            ("%2", ("%2", "beta", R.WORKER)),
        ):
            h = ros.lookup(name)
            self.assertEqual((h.pane, h.name, h.kind), want, name)

    def test_refuses(self):
        ros = roster(ORCH, ALPHA, BETA)
        for name, msg in (
            ("", "no worker name given"),
            ("gamma", "no worker 'gamma' in fleet session 'fleet'"),
            ("%77", "no worker '%77' in fleet session 'fleet'"),  # unknown, or a pane of another session
            ("orch", "'orch' is the orchestrator's pane, not a worker"),
            ("%0", "'%0' resolves to this pane"),
            # tmux would resolve these (session prefix, window index, "{last}") to some pane, possibly ours
            ("fle", "no worker"), ("fleet", "no worker"), ("0", "no worker"), ("fleet:0", "no worker"),
            ("{last}", "no worker"), ("=fleet:orch", "no worker"),
        ):
            with self.assertRaisesRegex(FleetError, re_escape(msg), msg=name):
                ros.lookup(name)

    def test_orchestrator_that_is_not_our_pane(self):
        # run from another terminal (%9): %0 is not our pane, but it is still an orchestrator
        ros = roster(ORCH, ALPHA, own="%9")
        for name in ("orch", "%0"):
            with self.assertRaisesRegex(FleetError, "orchestrator's pane", msg=name):
                ros.lookup(name)

    def test_orchestrator_by_role_under_another_name(self):
        ros = roster(pane("%4", "boss", role="orch"), ALPHA, own="%3")
        for name in ("boss", "%4"):
            with self.assertRaisesRegex(FleetError, "orchestrator's pane", msg=name):
                ros.lookup(name)

    def test_own_pane_that_is_a_worker(self):
        # a worker driving fleet: its own window is never a target
        ros = roster(ORCH, ALPHA, BETA, own="%1")
        for name in ("alpha", "%1"):
            with self.assertRaisesRegex(FleetError, "resolves to this pane", msg=name):
                ros.lookup(name)


class MultiPaneWindows(unittest.TestCase):
    """The user split a window: a name resolves to the pane that carries it."""

    def test_marked_pane_among_several(self):
        for order in ((pane("%5", "alpha", command="zsh"), ALPHA), (ALPHA, pane("%5", "alpha", command="zsh"))):
            h = roster(ORCH, *order).lookup("alpha")
            self.assertEqual((h.pane, h.kind), ("%1", R.WORKER))

    def test_single_unmarked_pane(self):
        h = roster(ORCH, pane("%6", "byhand"), tracked=()).lookup("byhand")
        self.assertEqual((h.pane, h.name, h.kind), ("%6", "byhand", R.UNTRACKED))

    def test_several_unmarked_panes(self):
        ros = roster(ORCH, pane("%6", "byhand"), pane("%7", "byhand", command="zsh"))
        with self.assertRaisesRegex(FleetError, "window 'byhand' has 2 panes and none is the worker"):
            ros.lookup("byhand")

    def test_orchestrator_window_split(self):
        ros = roster(ORCH, pane("%8", "orch", command="zsh"), ALPHA, own="%9")
        with self.assertRaisesRegex(FleetError, "orchestrator's pane"):
            ros.lookup("orch")

    def test_pane_id_picks_that_pane(self):
        h = roster(ORCH, ALPHA, pane("%5", "alpha", command="zsh")).lookup("%5")
        self.assertEqual((h.pane, h.kind), ("%5", R.OTHER))


class Kinds(unittest.TestCase):
    """orchestrator / worker (tracked) / untracked pane (claude, no record) / other."""

    PANES = (ORCH, ALPHA, BETA, pane("%6", "byhand"), pane("%7", "sh", command="zsh"),
             pane("%8", "named", command="claude"), pane("%9", "gone", fleet_name="gone"))

    def test_workers_and_untracked(self):
        ros = roster(*self.PANES)
        self.assertEqual([(h.pane, h.name) for h in ros.workers()], [("%1", "alpha"), ("%2", "beta")])
        # a claude whose record is gone is an untracked pane too
        self.assertEqual([h.name for h in ros.untracked()], ["byhand", "named", "gone"])

    def test_worker_lookup(self):
        ros = roster(*self.PANES)
        self.assertEqual(ros.worker("alpha").pane, "%1")
        for name in ("byhand", "sh"):
            with self.assertRaisesRegex(FleetError, "'%s' is not a tracked worker \\(no hook state\\)" % name):
                ros.worker(name)
        with self.assertRaisesRegex(FleetError, "orchestrator's pane"):
            ros.worker("orch")


class NewName(unittest.TestCase):
    def test_rules(self):
        ros = roster(ORCH, ALPHA)
        for name, msg in (
            ("a/b", "contains '/'"),
            ("orch", "'orch' is the orchestrator's window"),
            ("%3", "'%3' cannot be a worker name"),
            ("-x", "'-x' cannot be a worker name"),
            ("alpha", "a window named 'alpha' already runs in 'fleet'"),
        ):
            with self.assertRaisesRegex(FleetError, re_escape(msg), msg=name):
                ros.check_new_name(name)
        ros.check_new_name("gamma")


class Session(unittest.TestCase):
    """FLEET_SESSION, else the caller's own session if it is a fleet session, else the only one."""

    IN_TMUX = {"TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%0"}

    def find(self, environ, own_session="", running=()):
        def session_of(p):
            if not own_session:
                raise FleetError("can't find pane %s" % p)
            return own_session
        return R.find_session(environ, session_of, lambda: list(running))

    def test_found(self):
        for environ, own, running, want in (
            (self.IN_TMUX, "fleet", ["fleet", "fleet-x"], "fleet"),
            (self.IN_TMUX, "fleet-x", ["fleet", "fleet-x"], "fleet-x"),
            (dict(self.IN_TMUX, FLEET_SESSION="fleet-y"), "fleet", ["fleet"], "fleet-y"),
            ({}, "", ["work", "fleet"], "fleet"),  # from outside tmux
            (self.IN_TMUX, "work", ["work", "fleet-a"], "fleet-a"),  # from a non-fleet session
            (self.IN_TMUX, "", ["fleet"], "fleet"),  # own pane unknown to tmux
            (self.IN_TMUX, "fleetish", ["fleetish", "fleet"], "fleet"),
        ):
            self.assertEqual(self.find(environ, own, running), want, (environ, own, running))

    def test_not_found(self):
        for running, msg in (
            ([], "no fleet session running; start one with: fleet up [name]"),
            (["work"], "no fleet session running"),
            (["work", "fleet", "fleet-two"], "several fleet sessions running (fleet fleet-two); set FLEET_SESSION"),
        ):
            with self.assertRaisesRegex(FleetError, re_escape(msg), msg=running):
                self.find(self.IN_TMUX, "work", running)

    def test_own_session_asked_only_inside_tmux(self):
        asked = []
        R.find_session({}, lambda p: asked.append(p), lambda: ["fleet"])
        self.assertEqual(asked, [])


re_escape = re.escape
