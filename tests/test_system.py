"""The tmux adapter with the process runner swapped out: what it asks tmux, how it parses the
answers, and the pane guards its Terminal enforces."""

import subprocess
import unittest

import fakes  # noqa: F401  (puts scripts/ on sys.path)

from fleetlib.errors import FleetError
from fleetlib.system import Tmux


class Runner:
    """Stands in for running a process: records every argv, answers from a table of
    (argv prefix -> (returncode, stdout))."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def __call__(self, argv, check=True, timeout=None):
        self.calls.append(tuple(argv))
        rc, out = 0, ""
        for prefix, answer in self.answers.items():
            if tuple(argv[:len(prefix)]) == prefix:
                rc, out = answer
        if check and rc != 0:
            raise FleetError("%s failed (exit %d)" % (" ".join(argv[:3]), rc))
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    def keys(self):
        return [c for c in self.calls if c[1] == "send-keys"]


ROLE = ("tmux", "display", "-p", "-t")


def tmux(own_pane="%0", roles=None, answers=None):
    """A Tmux whose panes have the given @fleet_role (default: none is an orchestrator)."""
    run = Runner(answers)
    roles = roles or {}
    real = run.__call__

    def call(argv, check=True, timeout=None):
        if tuple(argv[:4]) == ROLE and argv[-1] == "#{@fleet_role}":
            run.calls.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=roles.get(argv[4], "") + "\n", stderr="")
        return real(argv, check=check, timeout=timeout)
    t = Tmux(own_pane=own_pane, run=call)
    return t, run


class Panes(unittest.TestCase):
    def test_panes_parses_the_tab_separated_listing(self):
        out = "orch\t%0\t2.1.329\t\torch\nalpha\t%1\t2.1.329\talpha\t\nalpha\t%5\tzsh\t\t\n"
        t, run = tmux(answers={("tmux", "list-panes"): (0, out)})
        self.assertEqual(t.panes("fleet"), [
            {"window_name": "orch", "pane_id": "%0", "pane_current_command": "2.1.329", "@fleet_name": "",
             "@fleet_role": "orch"},
            {"window_name": "alpha", "pane_id": "%1", "pane_current_command": "2.1.329", "@fleet_name": "alpha",
             "@fleet_role": ""},
            {"window_name": "alpha", "pane_id": "%5", "pane_current_command": "zsh", "@fleet_name": "",
             "@fleet_role": ""},
        ])
        self.assertEqual(run.calls[0][:5], ("tmux", "list-panes", "-s", "-t", "=fleet"))

    def test_all_panes_maps_pane_ids_to_fleet_names(self):
        t, _ = tmux(answers={("tmux", "list-panes"): (0, "%0\t\n%1\talpha\n\n%7\tbeta\n")})
        self.assertEqual(t.all_panes(), {"%0": "", "%1": "alpha", "%7": "beta"})

    def test_all_panes_is_none_when_tmux_cannot_be_asked(self):
        t, _ = tmux(answers={("tmux", "list-panes"): (1, "")})
        self.assertIsNone(t.all_panes())


class TerminalKeys(unittest.TestCase):
    def test_type_sends_each_character_literally_at_the_pace(self):
        slept = []
        t, run = tmux()
        term = t.terminal("%1", delay=0.05, sleep=slept.append)
        term.type("hi")
        self.assertEqual(run.keys(), [("tmux", "send-keys", "-t", "%1", "-l", "h"),
                                      ("tmux", "send-keys", "-t", "%1", "-l", "i")])
        self.assertEqual(slept, [0.05, 0.05])

    def test_type_unpaced_sends_the_text_at_once(self):
        t, run = tmux()
        t.terminal("%1", sleep=self.fail).type("claude --x", paced=False)
        self.assertEqual(run.keys(), [("tmux", "send-keys", "-t", "%1", "-l", "claude --x")])

    def test_submit_is_enter_and_submit_now_is_csi_u_ctrl_enter(self):
        t, run = tmux()
        term = t.terminal("%1")
        term.submit()
        term.submit(now=True)
        self.assertEqual(run.keys(), [("tmux", "send-keys", "-t", "%1", "Enter"),
                                      ("tmux", "send-keys", "-t", "%1", "-l", "\x1b[13;5u")])

    def test_press_sends_named_keys(self):
        t, run = tmux()
        t.terminal("%1").press("Down", "Enter")
        self.assertEqual(run.keys(), [("tmux", "send-keys", "-t", "%1", "Down", "Enter")])

    def test_orchestrator_role_is_asked_once_per_terminal(self):
        t, run = tmux()
        term = t.terminal("%1")
        term.type("ab")
        term.submit()
        self.assertEqual(len([c for c in run.calls if c[:2] == ("tmux", "display")]), 1)


class TerminalScreen(unittest.TestCase):
    def test_screen_is_the_parsed_ansi_capture(self):
        t, run = tmux(answers={("tmux", "capture-pane"): (0, "hello\n")})
        scr = t.terminal("%1").screen()
        self.assertEqual(run.calls, [("tmux", "capture-pane", "-p", "-t", "%1", "-e")])
        self.assertEqual(scr.tail(1), ["hello"])

    def test_scrollback_is_plain_text_with_history(self):
        t, run = tmux(answers={("tmux", "capture-pane"): (0, "a\nb\n")})
        self.assertEqual(t.terminal("%1").scrollback(60), "a\nb\n")
        self.assertEqual(run.calls, [("tmux", "capture-pane", "-p", "-t", "%1", "-S", "-60")])


class Guards(unittest.TestCase):
    """Keys never go to an empty target, a non-pane-id, the caller's own pane, or an orchestrator's
    pane (unless the Terminal was opened for `fleet up`)."""

    KEYS = (lambda term: term.type("x"), lambda term: term.submit(), lambda term: term.submit(now=True),
            lambda term: term.press("Enter"))

    def refused(self, t, pane, pattern, **kw):
        for key in self.KEYS:
            with self.assertRaisesRegex(FleetError, pattern, msg=pane):
                key(t.terminal(pane, **kw))

    def test_empty_target(self):
        t, run = tmux()
        with self.assertRaisesRegex(FleetError, "empty tmux target"):
            t.terminal("")
        self.assertEqual(run.calls, [])

    def test_not_a_pane_id(self):
        t, run = tmux()
        self.refused(t, "fleet:0", "pane ids only")
        self.assertEqual(run.keys(), [])

    def test_own_pane(self):
        t, run = tmux(own_pane="%3")
        self.refused(t, "%3", "the caller's own")
        self.refused(t, "%3", "the caller's own", orchestrator_ok=True)
        self.assertEqual(run.keys(), [])

    def test_orchestrator_pane_even_when_not_our_own(self):
        t, run = tmux(own_pane="%9", roles={"%0": "orch"})
        self.refused(t, "%0", "an orchestrator's pane")
        self.assertEqual(run.keys(), [])

    def test_orchestrator_pane_when_opened_for_it(self):
        t, run = tmux(own_pane="", roles={"%0": "orch"})
        term = t.terminal("%0", orchestrator_ok=True)
        term.type("claude", paced=False)
        term.submit()
        self.assertEqual(len(run.keys()), 2)
        self.assertEqual([c for c in run.calls if c[:2] == ("tmux", "display")], [])


if __name__ == "__main__":
    unittest.main()
