"""Commands against fake tmux/git/wt: argument parsing, target resolution, send, wait, broadcast,
status, keys, rm, new, up."""

import io
import json
import os
import shutil
import tempfile
import unittest

from fakes import Clock, FakeTmux, box_screen, fixture, make_ctx

from fleetlib import args as A
from fleetlib import cli
from fleetlib import lifecycle as lc
from fleetlib import state as st
from fleetlib.errors import FleetError


class Args(unittest.TestCase):
    def test_up(self):
        self.assertEqual(A.parse_up([]), {"name": "", "detach": False, "mode": "auto", "extra": []})
        self.assertEqual(A.parse_up(["x", "--detach", "--mode", "manual", "--", "--model", "opus"]),
                         {"name": "x", "detach": True, "mode": "manual", "extra": ["--model", "opus"]})
        for bad in (["a", "b"], ["--nope"], ["--mode"]):
            with self.assertRaises(A.UsageError):
                A.parse_up(bad)

    def test_new(self):
        o = A.parse_new(["repo", "feat", "--base", "origin/dev", "--mode", "auto", "--prompt-file", "p.md",
                         "--no-worktree", "--", "--model", "haiku", "--base"])
        self.assertEqual(o, {"src": "repo", "name": "feat", "worktree": False, "base": "origin/dev",
                             "mode": "auto", "prompt_file": "p.md", "prompt_stdin": False,
                             "extra": ["--model", "haiku", "--base"]})
        self.assertEqual(A.parse_new(["--mode", "plan", "r", "b"])["mode"], "plan")
        self.assertTrue(A.parse_new(["r", "b", "--prompt-stdin"])["prompt_stdin"])
        for bad in (["repo"], [], ["r", "b", "c"], ["r", "b", "--base"], ["r", "b", "--zap"], ["r", "b", "--mode", ""],
                    ["r", "b", "--prompt-stdin", "--prompt-file", "p.md"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_new(bad)

    def test_status(self):
        self.assertEqual(A.parse_status([]), {"json": False})
        self.assertEqual(A.parse_status(["--json"]), {"json": True})
        with self.assertRaises(A.UsageError):
            A.parse_status(["--yaml"])

    def test_send(self):
        self.assertEqual(A.parse_send(["w", "hello", "world"]),
                         {"now": False, "force": False, "prompt_file": "", "prompt_stdin": False, "name": "w",
                          "text": "hello world"})
        self.assertEqual(A.parse_send(["--now", "--force", "w", "a\nb"]),
                         {"now": True, "force": True, "prompt_file": "", "prompt_stdin": False, "name": "w",
                          "text": "a b"})
        self.assertEqual(A.parse_send(["--prompt-stdin", "--now", "w"]),
                         {"now": True, "force": False, "prompt_file": "", "prompt_stdin": True, "name": "w",
                          "text": ""})
        self.assertEqual(A.parse_send(["--prompt-file", "p.md", "w"])["prompt_file"], "p.md")
        # after the worker name, everything is text, flags included
        self.assertEqual(A.parse_send(["w", "--now", "please"])["text"], "--now please")
        self.assertFalse(A.parse_send(["w", "--now", "please"])["now"])
        for bad in ([], ["w"], ["--now", "w"], ["--zap", "w", "x"], ["w", "   "], ["--prompt-stdin"],
                    ["--prompt-file"], ["--prompt-file", "", "w"], ["--prompt-file", "p.md"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_send(bad)
        for bad in (["--prompt-stdin", "w", "text"], ["--prompt-file", "p.md", "w", "text"],
                    ["--prompt-file", "p.md", "--prompt-stdin", "w"]):
            with self.assertRaisesRegex(A.UsageError, "one way only", msg=bad):
                A.parse_send(bad)

    def test_broadcast(self):
        o = A.parse_broadcast(["--to", "a,b,", "--wait", "30", "--now", "do", "it"])
        self.assertEqual(o, {"now": True, "force": False, "prompt_file": "", "prompt_stdin": False,
                             "to": ["a", "b"], "wait": 30, "text": "do it"})
        self.assertEqual(A.parse_broadcast(["--prompt-file", "p.md"])["prompt_file"], "p.md")
        o = A.parse_broadcast(["--prompt-stdin", "--to", "a"])
        self.assertEqual((o["prompt_stdin"], o["to"], o["text"]), (True, ["a"], ""))
        self.assertEqual(A.parse_broadcast(["--", "--to", "x"])["text"], "--to x")
        self.assertIsNone(A.parse_broadcast(["x"])["wait"])
        for bad in ([], ["--to"], ["--to", ","], ["--wait"], ["--wait", "soon", "x"], ["--wait", "-1", "x"],
                    ["--zap", "x"], ["--now"], ["--prompt-stdin", "x"], ["--prompt-file", "p.md", "x"],
                    ["--prompt-file", "p.md", "--prompt-stdin"], ["--prompt-file"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_broadcast(bad)

    def test_wait_read_last(self):
        self.assertEqual(A.parse_wait(["w"]), {"name": "w", "timeout": 600})
        self.assertEqual(A.parse_wait(["w", "5"]), {"name": "w", "timeout": 5})
        self.assertEqual(A.parse_read(["w"]), {"name": "w", "n": 60})
        self.assertEqual(A.parse_last(["w", "3"]), {"name": "w", "n": 3})
        for fn in (A.parse_wait, A.parse_read, A.parse_last):
            for bad in ([], [""], ["w", "x"], ["w", "1", "2"], ["w", "-3"]):
                with self.assertRaises(A.UsageError, msg=(fn.__name__, bad)):
                    fn(bad)

    def test_keys_rm(self):
        self.assertEqual(A.parse_keys(["w", "Down", "Enter"]), {"name": "w", "keys": ["Down", "Enter"]})
        with self.assertRaises(A.UsageError):
            A.parse_keys(["w"])
        self.assertEqual(A.parse_rm(["--force", "w"]), {"force": True, "name": "w"})
        self.assertEqual(A.parse_rm(["w", "--force"]), {"force": True, "name": "w"})
        for bad in ([], ["--force"], ["a", "b"], ["--zap", "a"]):
            with self.assertRaises(A.UsageError, msg=bad):
                A.parse_rm(bad)

    def test_help_and_unknown_command(self):
        for argv in ([], ["help"], ["bogus"]):
            ctx = make_ctx()
            self.assertEqual(cli.run(ctx, argv), 0)
            self.assertTrue(ctx.out.getvalue().startswith("fleet up [name]"))

    def test_usage_error_exit_code_and_message(self):
        ctx = make_ctx()
        self.assertEqual(cli.run(ctx, ["send", "w"]), 1)
        self.assertEqual(ctx.err.getvalue(), "fleet: usage: fleet send [--now] [--force] [--prompt-file P | --prompt-stdin] <worker> [<text>]\n")


class Fleet(unittest.TestCase):
    """A fake tmux server: session 'fleet' with the orchestrator (%0, our own pane) and two workers,
    plus another session 'other' with a pane of its own."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fleet-test-")
        self.tmux = FakeTmux(own_pane="%0")
        self.orch = self.tmux.add("fleet", "orch", "%0", role="orch")
        self.a = self.tmux.add("fleet", "alpha", "%1", fleet_name="alpha")
        self.b = self.tmux.add("fleet", "beta", "%2", fleet_name="beta")
        self.x = self.tmux.add("other", "shell", "%9", command="zsh")
        self.clock = Clock()
        self.ctx = make_ctx(self.tmux, self.dir, clock=self.clock)
        self.ctx.store.ensure()
        for p in (self.a, self.b):
            self.track(p, state="idle")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def track(self, pane, **fields):
        rec = {"name": pane.window, "session": pane.session, "pane": pane.id, "dir": "/w", "repo": "",
               "worktree": "", "branch": "", "mode": "manual", "state": "idle", "detail": "",
               "ts": self.clock.now(), "created": self.clock.now()}
        rec.update(fields)
        st.Store.write(self.ctx.store.path(pane.id), rec)

    def rec(self, pane):
        return st.Store.load(self.ctx.store.path(pane.id))

    def run_cmd(self, *argv, ctx=None):
        ctx = ctx or self.ctx
        ctx.out.seek(0)
        ctx.out.truncate()
        ctx.err.seek(0)
        ctx.err.truncate()
        code = cli.run(ctx, list(argv))
        return code, ctx.out.getvalue(), ctx.err.getvalue()


class Session(Fleet):
    def test_inside_the_fleet_session(self):
        self.assertEqual(cli.session(self.ctx), "fleet")

    def test_fleet_session_env_wins(self):
        self.ctx.environ["FLEET_SESSION"] = "fleet-x"
        self.assertEqual(cli.session(self.ctx), "fleet-x")

    def test_only_fleet_session_from_outside(self):
        del self.ctx.environ["TMUX"]
        self.assertEqual(cli.session(self.ctx), "fleet")

    def test_several_sessions(self):
        self.ctx.environ["TMUX_PANE"] = "%9"  # in a non-fleet session
        self.tmux.extra_sessions.append("fleet-two")
        with self.assertRaisesRegex(FleetError, r"several fleet sessions running \(fleet fleet-two\)"):
            cli.session(self.ctx)

    def test_no_session(self):
        tmux = FakeTmux(own_pane="")
        tmux.add("work", "w", "%3")
        ctx = make_ctx(tmux, self.dir, environ={"TMUX_PANE": "", "TMUX": ""})
        with self.assertRaisesRegex(FleetError, "no fleet session running"):
            cli.session(ctx)


class Target(Fleet):
    def test_worker_name(self):
        self.assertEqual(cli.target(self.ctx, "alpha"), "%1")

    def test_unknown_name(self):
        with self.assertRaisesRegex(FleetError, "no worker 'gamma' in fleet session 'fleet'"):
            cli.target(self.ctx, "gamma")

    def test_empty_name(self):
        with self.assertRaises(FleetError):
            cli.target(self.ctx, "")

    def test_own_pane_by_window_name(self):
        with self.assertRaisesRegex(FleetError, "resolves to this pane"):
            cli.target(self.ctx, "orch")

    def test_own_pane_by_id(self):
        with self.assertRaisesRegex(FleetError, "resolves to this pane"):
            cli.target(self.ctx, "%0")

    def test_orchestrator_from_outside_the_fleet(self):
        # run from another terminal: %0 is not our pane, but it is still the orchestrator
        self.ctx.environ["TMUX_PANE"] = "%9"
        self.ctx.environ["FLEET_SESSION"] = "fleet"
        self.tmux.own_pane = "%9"
        with self.assertRaisesRegex(FleetError, "orchestrator's pane"):
            cli.target(self.ctx, "orch")
        with self.assertRaisesRegex(FleetError, "orchestrator's pane"):
            cli.target(self.ctx, "%0")

    def test_raw_pane_id(self):
        self.assertEqual(cli.target(self.ctx, "%2"), "%2")

    def test_raw_pane_id_of_another_session(self):
        with self.assertRaisesRegex(FleetError, "no worker '%9' in fleet session 'fleet'"):
            cli.target(self.ctx, "%9")

    def test_orchestrator_of_another_fleet(self):
        # The live slip that shaped this rule: a test fleet (FLEET_SESSION=fleet-test) driven from a
        # worker pane (%3) of the real fleet; "%0" is the real fleet's orchestrator.
        tmux = FakeTmux(own_pane="%3")
        tmux.add("fleet", "orch", "%0", role="orch")
        tmux.add("fleet", "me", "%3", fleet_name="me")
        tmux.add("fleet-test", "orch", "%4", role="orch")
        tmux.add("fleet-test", "w1", "%5", fleet_name="w1")
        ctx = make_ctx(tmux, self.dir, environ={"TMUX_PANE": "%3", "FLEET_SESSION": "fleet-test"})
        self.assertEqual(cli.target(ctx, "w1"), "%5")
        for name in ("%0", "%3", "%4", "orch", "me"):
            with self.assertRaises(FleetError, msg=name):
                cli.target(ctx, name)
        for argv in (["send", "%0", "hi"], ["keys", "%0", "Enter"], ["broadcast", "--to", "%0,orch,%3", "hi"]):
            cli.run(ctx, argv)
        self.assertEqual([c for c in tmux.calls if c and c[0] == "send-keys"], [])

    def test_unknown_pane_id(self):
        with self.assertRaisesRegex(FleetError, "no worker '%77'"):
            cli.target(self.ctx, "%77")

    def test_other_tmux_targets_are_not_resolved(self):
        # tmux would resolve these (session prefix, window index, "{last}") to some pane, possibly ours
        for name in ("fle", "fleet", "0", "fleet:0", "{last}", "=fleet:orch", "other"):
            with self.assertRaises(FleetError, msg=name):
                cli.target(self.ctx, name)

    def test_no_session_and_unknown_name(self):
        tmux = FakeTmux(own_pane="")
        ctx = make_ctx(tmux, self.dir, environ={"TMUX_PANE": "", "TMUX": ""})
        with self.assertRaisesRegex(FleetError, "no fleet session running"):
            cli.target(ctx, "alpha")

    def test_no_command_types_into_the_orchestrator(self):
        for argv in (["send", "orch", "hi"], ["send", "%0", "hi"], ["keys", "orch", "Enter"],
                     ["keys", "%0", "Enter"], ["broadcast", "--to", "orch,%0", "hi"], ["rm", "%0"],
                     ["send", "nobody", "hi"], ["keys", "", "Enter"], ["read", "orch"]):
            code, _, err = self.run_cmd(*argv)
            self.assertNotEqual(code, 0, argv)
            self.assertTrue(err.startswith("fleet: "), argv)
        self.assertEqual(self.tmux.keys_sent_to("%0"), [])
        self.assertEqual(self.tmux.keys_sent_to(""), [])

    def test_system_layer_refuses_orchestrators_even_when_not_our_pane(self):
        self.tmux.own_pane = "%9"
        for send in (lambda: self.tmux.send_keys("%0", "Enter"), lambda: self.tmux.send_literal("%0", "x")):
            with self.assertRaisesRegex(FleetError, "an orchestrator's pane"):
                send()
        self.assertEqual(self.tmux.keys_sent_to("%0"), [])

    def test_system_layer_refuses_own_and_empty_panes(self):
        for pane in ("%0", "", "fleet:0"):
            with self.assertRaises(FleetError, msg=pane):
                self.tmux.send_keys(pane, "Enter")
            with self.assertRaises(FleetError, msg=pane):
                self.tmux.send_literal(pane, "x")


class Send(Fleet):
    def test_types_verifies_and_submits(self):
        code, out, err = self.run_cmd("send", "alpha", "fix", "the", "bug")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "sent to alpha: fix the bug\n")
        self.assertEqual(self.a.submitted, [("fix the bug", "enter")])
        # typed one character at a time
        literals = [c for c in self.tmux.keys_sent_to("%1") if "-l" in c]
        self.assertEqual(len(literals), len("fix the bug"))
        r = self.rec(self.a)
        self.assertEqual((r["state"], r["sent_at"]), ("busy", self.clock.now()))
        self.assertEqual(self.tmux.window_options[("%1", "@fleet_state")], "busy")

    def test_now_sends_ctrl_enter(self):
        code, out, _ = self.run_cmd("send", "--now", "alpha", "stop that")
        self.assertEqual(code, 0)
        self.assertEqual(out, "sent (now) to alpha: stop that\n")
        self.assertEqual(self.a.submitted, [("stop that", "now")])

    def test_refuses_typed_text(self):
        self.a.screen = fixture("typed_text.ansi")
        code, _, err = self.run_cmd("send", "alpha", "hi")
        self.assertEqual(code, 1)
        self.assertIn("prompt box already holds: 'hello typed text'", err)
        self.assertEqual(self.tmux.keys_sent_to("%1"), [])

    def test_force_appends_to_typed_text(self):
        self.a.typed = "draft "
        code, _, _ = self.run_cmd("send", "--force", "alpha", "and more")
        self.assertEqual(code, 0)
        self.assertEqual(self.a.submitted, [("draft and more", "enter")])

    def test_suggested_prompt_is_not_typed_text(self):
        self.a.screens = [fixture("suggested_prompt.ansi")]
        code, out, err = self.run_cmd("send", "alpha", "next task")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.a.submitted, [("next task", "enter")])

    def test_refuses_dialogs(self):
        for fx in ("permission_dialog.ansi", "ask_user_question.ansi", "trust_dialog.ansi"):
            self.a.screen = fixture(fx)
            for flags in ([], ["--now"], ["--force"]):
                code, out, err = self.run_cmd("send", *(flags + ["alpha", "hi"]))
                self.assertEqual(code, 1, (fx, flags))
                self.assertIn("shows a dialog", err)
                self.assertIn("Esc to cancel", out)  # the dialog is shown
        self.assertEqual(self.tmux.keys_sent_to("%1"), [])

    def test_refuses_without_a_prompt_box(self):
        self.a.screen = "user@host ~ $ \n"
        code, _, err = self.run_cmd("send", "alpha", "hi")
        self.assertEqual(code, 1)
        self.assertIn("no claude prompt box", err)
        self.assertEqual(self.tmux.keys_sent_to("%1"), [])

    def test_does_not_submit_unseen_text(self):
        self.a.echo = False
        code, _, err = self.run_cmd("send", "alpha", "hello")
        self.assertEqual(code, 1)
        self.assertIn("typed text not visible", err)
        self.assertEqual(self.a.submitted, [])
        self.assertEqual(self.rec(self.a)["state"], "idle")

    def test_text_shown_late_is_accepted(self):
        # the box redraws slowly: first capture after typing still shows it empty
        self.a.screens = [box_screen(), box_screen(), box_screen()]
        code, _, _ = self.run_cmd("send", "alpha", "hello")
        self.assertEqual(code, 0)

    def test_untracked_worker_gets_no_state(self):
        c = self.tmux.add("fleet", "manual", "%5")
        code, _, _ = self.run_cmd("send", "manual", "hi")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.ctx.store.path("%5")))
        self.assertEqual(c.submitted, [("hi", "enter")])


class PromptStdin(Fleet):
    PROMPT = "# Task\n\nRebase on main.\nThen run the tests.\n"

    def saved_prompt(self, sent):
        prefix, suffix = "Read ", " and do what it says."
        self.assertTrue(sent.startswith(prefix) and sent.endswith(suffix), sent)
        path = sent[len(prefix):-len(suffix)]
        self.assertEqual(os.path.dirname(path), os.path.join(self.dir, "prompts"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.PROMPT)  # newlines kept
        return path

    def test_send(self):
        self.ctx.stdin = io.StringIO(self.PROMPT)
        code, _, err = self.run_cmd("send", "--prompt-stdin", "alpha")
        self.assertEqual((code, err), (0, ""))
        [(sent, how)] = self.a.submitted
        self.assertEqual(how, "enter")
        self.saved_prompt(sent)

    def test_broadcast_shares_one_file(self):
        self.ctx.stdin = io.StringIO(self.PROMPT)
        code, out, err = self.run_cmd("broadcast", "--prompt-stdin")
        self.assertEqual((code, err, out), (0, "", "sent to alpha\nsent to beta\n"))
        self.assertEqual(self.a.submitted, self.b.submitted)
        self.saved_prompt(self.a.submitted[0][0])
        self.assertEqual(len(os.listdir(os.path.join(self.dir, "prompts"))), 1)

    def test_prompt_file(self):
        pf = os.path.join(self.dir, "task.md")
        open(pf, "w").close()
        rel = os.path.relpath(pf)
        code, _, err = self.run_cmd("send", "--prompt-file", rel, "alpha")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.a.submitted, [("Read %s and do what it says." % pf, "enter")])  # made absolute
        code, out, err = self.run_cmd("broadcast", "--prompt-file", pf)
        self.assertEqual((code, err, out), (0, "", "sent to alpha\nsent to beta\n"))
        self.assertEqual(self.b.submitted, [("Read %s and do what it says." % pf, "enter")])

    def test_missing_prompt_file(self):
        for argv in (["send", "--prompt-file", "/nope.md", "alpha"], ["broadcast", "--prompt-file", "/nope.md"]):
            code, _, err = self.run_cmd(*argv)
            self.assertEqual((code, err), (1, "fleet: no such prompt file: /nope.md\n"), argv)
        self.assertEqual(self.tmux.keys_sent_to("%1"), [])

    def test_empty_stdin(self):
        for argv in (["send", "--prompt-stdin", "alpha"], ["broadcast", "--prompt-stdin"]):
            self.ctx.stdin = io.StringIO("  \n")
            code, _, err = self.run_cmd(*argv)
            self.assertEqual((code, err), (1, "fleet: --prompt-stdin: stdin is empty; nothing to send\n"), argv)
        self.assertEqual(self.tmux.keys_sent_to("%1"), [])

    def test_terminal_stdin(self):
        tty = io.StringIO("")
        tty.isatty = lambda: True
        self.ctx.stdin = tty
        code, _, err = self.run_cmd("send", "--prompt-stdin", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("stdin is a terminal", err)


class Wait(Fleet):
    def test_done_after_send(self):
        self.track(self.a, state="busy", sent_at=self.clock.now(), ts=self.clock.now())

        def tick(t):
            if t > self.clock.t - 1 and self.clock.now() >= 1_000_005:
                st.Store.update(self.ctx.store.path("%1"), {"state": "done", "event": "Stop", "ts": self.clock.now()})
        self.a.screen = fixture("busy.ansi")
        self.clock.on_sleep = tick
        code, out, _ = self.run_cmd("wait", "alpha", "60")
        self.assertEqual((code, out), (0, "done\n"))

    def test_stale_done_from_the_previous_turn_is_ignored(self):
        # The previous turn's Stop at t-10; we sent at t; the hook has not caught up yet.
        now = self.clock.now()
        self.track(self.a, state="done", event="Stop", ts=now - 10, sent_at=now)
        self.a.screen = fixture("busy.ansi")
        code, out, _ = self.run_cmd("wait", "alpha", "5")
        self.assertEqual((code, out), (2, "still busy after 5s\n"))

    def test_blocked(self):
        self.track(self.a, state="blocked", detail="permission: Bash", ts=self.clock.now())
        code, out, _ = self.run_cmd("wait", "alpha", "5")
        self.assertEqual((code, out), (4, "blocked: permission: Bash. See it with: fleet read alpha\n"))

    def test_busy_turns_blocked_from_the_screen(self):
        self.track(self.a, state="busy", ts=self.clock.now() - 10)
        self.a.screen = fixture("permission_dialog.ansi")
        code, out, _ = self.run_cmd("wait", "alpha", "5")
        self.assertEqual((code, out), (4, "blocked: dialog on screen. See it with: fleet read alpha\n"))
        self.assertEqual(self.rec(self.a)["state"], "blocked")

    def test_interrupt_is_seen(self):
        self.track(self.a, state="busy", ts=self.clock.now() - 10)
        self.a.screen = fixture("interrupted.ansi")
        code, out, _ = self.run_cmd("wait", "alpha", "5")
        self.assertEqual((code, out), (0, "idle\n"))
        self.assertEqual(self.rec(self.a)["detail"], "interrupted")

    def test_exited(self):
        self.track(self.a, state="exited")
        self.assertEqual(self.run_cmd("wait", "alpha")[:2], (0, "exited\n"))

    def test_untracked(self):
        self.tmux.add("fleet", "manual", "%5")
        code, _, err = self.run_cmd("wait", "manual")
        self.assertEqual((code, err), (1, "fleet: 'manual' is not a tracked worker (no hook state)\n"))

    def test_timeout_is_wall_clock(self):
        self.track(self.a, state="busy", ts=self.clock.now())
        self.a.screen = fixture("busy.ansi")
        start = self.clock.t
        code, _, _ = self.run_cmd("wait", "alpha", "30")
        self.assertEqual(code, 2)
        self.assertLess(self.clock.t - start, 32)


class Broadcast(Fleet):
    def test_to_all_tracked_workers(self):
        code, out, err = self.run_cmd("broadcast", "rebase on main")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "sent to alpha\nsent to beta\n")
        self.assertEqual(self.a.submitted, [("rebase on main", "enter")])
        self.assertEqual(self.b.submitted, [("rebase on main", "enter")])

    def test_a_refusal_skips_only_that_worker(self):
        self.a.screen = fixture("typed_text.ansi")
        code, out, err = self.run_cmd("broadcast", "--to", "alpha,ghost,beta", "go")
        self.assertEqual(code, 1)
        self.assertEqual(out, "NOT sent to alpha (reason above)\nNOT sent to ghost (reason above)\nsent to beta\n")
        self.assertIn("fleet: prompt box already holds", err)
        self.assertIn("fleet: no worker 'ghost'", err)
        self.assertEqual(self.b.submitted, [("go", "enter")])

    def test_orchestrator_is_never_a_recipient(self):
        code, out, _ = self.run_cmd("broadcast", "--to", "orch", "go")
        self.assertEqual(code, 1)
        self.assertEqual(self.tmux.keys_sent_to("%0"), [])

    def test_no_workers(self):
        for p in ("%1", "%2"):
            st.Store.remove(self.ctx.store.path(p))
        code, _, err = self.run_cmd("broadcast", "go")
        self.assertEqual((code, err), (1, "fleet: no workers to send to\n"))

    def test_exit_code_priority(self):
        self.assertEqual(cli.broadcast_code(False, False, False), 0)
        self.assertEqual(cli.broadcast_code(False, False, True), 2)
        self.assertEqual(cli.broadcast_code(False, True, True), 4)  # bash got 2 if the timeout came first
        self.assertEqual(cli.broadcast_code(True, True, True), 1)

    def test_wait_prints_replies_and_codes(self):
        tp = os.path.join(self.dir, "t.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "rebased"}]}}) + "\n")
        self.a.screen = fixture("busy.ansi")
        self.b.screen = fixture("busy.ansi")

        def tick(t):
            if self.a.submitted and self.rec(self.a)["state"] != "done":
                st.Store.update(self.ctx.store.path("%1"), {"state": "done", "event": "Stop",
                                                             "ts": self.clock.now(), "transcript_path": tp})
            if self.b.submitted and self.rec(self.b)["state"] != "blocked":
                st.Store.update(self.ctx.store.path("%2"), {"state": "blocked", "detail": "question",
                                                             "ts": self.clock.now()})
        self.a.screens = [box_screen()]
        self.b.screens = [box_screen()]
        self.a.screen = None
        self.b.screen = None
        self.clock.on_sleep = tick
        code, out, _ = self.run_cmd("broadcast", "--wait", "20", "rebase")
        self.assertEqual(code, 4)
        self.assertEqual(out, "sent to alpha\nsent to beta\n##### alpha: done\nrebased\n----\n"
                              "##### beta: BLOCKED, see it with: fleet read beta\n")


class Status(Fleet):
    def test_table(self):
        self.track(self.a, state="done", ts=self.clock.now() - 125, last_message="all\n  good")
        self.track(self.b, state="blocked", detail="permission: Bash and a very long detail",
                   ts=self.clock.now() - 1)
        self.b.screen = fixture("permission_dialog.ansi")
        self.tmux.add("fleet", "byhand", "%6")  # a claude started by hand
        self.tmux.add("fleet", "sh", "%7", command="zsh")
        code, out, _ = self.run_cmd("status")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [
            "fleet session: fleet",
            "WORKER                   STATE    AGE   DETAIL                 LAST MESSAGE",
            "alpha                    done     2m                           all good",
            "beta                     blocked  1s    permission: Bash and a ",
            "byhand                   untracked",
        ])

    def test_json(self):
        code, out, _ = self.run_cmd("status", "--json")
        d = json.loads(out)
        self.assertEqual([w["name"] for w in d["workers"]], ["alpha", "beta"])
        self.assertEqual(d["untracked"], [])

    def test_reconciles_and_sweeps(self):
        self.track(self.a, state="busy", ts=self.clock.now() - 10)
        self.a.screen = fixture("question_cancelled.ansi")
        # beta's pane was reused by something that is not beta
        self.b.fleet_name = ""
        # a worker of another session is left alone
        self.track(self.x, name="shell", session="other", state="busy")
        self.x.fleet_name = "shell"
        code, out, _ = self.run_cmd("status", "--json")
        d = json.loads(out)
        self.assertEqual([(w["name"], w["state"], w["detail"]) for w in d["workers"]],
                         [("alpha", "idle", "interrupted")])
        self.assertFalse(os.path.exists(self.ctx.store.path("%2")))
        self.assertTrue(os.path.exists(self.ctx.store.path("%9")))

    def test_no_sweep_when_tmux_cannot_be_asked(self):
        self.tmux.server_up = False
        cli.sweep(self.ctx)
        self.assertTrue(os.path.exists(self.ctx.store.path("%1")))

    def test_empty(self):
        for p in ("%1", "%2"):
            st.Store.remove(self.ctx.store.path(p))
        self.assertEqual(self.run_cmd("status")[1], "fleet session: fleet\n(no workers)\n")

    def test_age(self):
        self.assertEqual([cli.age(s) for s in (0, 59, 60, 3599, 3600, -5)], ["0s", "59s", "1m", "59m", "1h", "0s"])


class Keys(Fleet):
    def test_keys_unblock(self):
        self.track(self.a, state="blocked")
        code, _, _ = self.run_cmd("keys", "alpha", "Down", "Enter")
        self.assertEqual(code, 0)
        self.assertEqual(self.tmux.keys_sent_to("%1")[-1], ("send-keys", "-t", "%1", "Down", "Enter"))
        self.assertEqual(self.rec(self.a)["state"], "busy")

    def test_keys_leave_other_states(self):
        self.run_cmd("keys", "alpha", "Escape")
        self.assertEqual(self.rec(self.a)["state"], "idle")


class LastAndRead(Fleet):
    def test_last_from_transcript(self):
        tp = os.path.join(self.dir, "t.jsonl")
        lines = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "two"},
                                                          {"type": "text", "text": "lines"}]}},
        ]
        with open(tp, "w") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        self.track(self.a, transcript_path=tp)
        self.assertEqual(self.run_cmd("last", "alpha")[1], "two\nlines\n----\n")
        self.assertEqual(self.run_cmd("last", "alpha", "5")[1], "one\n----\ntwo\nlines\n----\n")

    def test_last_waits_for_the_transcript_to_catch_up(self):
        # Seen live: Stop fired (last_message "new") ~0.2s before the transcript had the reply.
        tp = os.path.join(self.dir, "t.jsonl")
        entry = lambda t: json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}) + "\n"
        with open(tp, "w") as f:
            f.write(entry("old"))
        self.track(self.a, transcript_path=tp, event="Stop", last_message="new")

        def flush(t):
            with open(tp, "a") as f:
                f.write(entry("new"))
            self.clock.on_sleep = None
        self.clock.on_sleep = flush
        self.assertEqual(self.run_cmd("last", "alpha")[1], "new\n----\n")

    def test_last_falls_back_to_the_hook_copy(self):
        tp = os.path.join(self.dir, "t.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}}) + "\n")
        self.track(self.a, transcript_path=tp, event="Stop", last_message="new")
        self.assertEqual(self.run_cmd("last", "alpha", "2")[1], "old\n----\nnew\n----\n")

    def test_last_without_transcript(self):
        self.assertEqual(self.run_cmd("last", "alpha")[1], "(no reply yet)\n")
        self.track(self.a, last_message="from the hook")
        self.assertEqual(self.run_cmd("last", "alpha")[1], "from the hook\n")

    def test_read_drops_blank_lines(self):
        self.a.screen = "a\n\nb\n\n"
        self.assertEqual(self.run_cmd("read", "alpha")[1], "a\nb\n")


class Rm(Fleet):
    def setUp(self):
        super().setUp()
        self.wt = os.path.join(self.dir, "repo", "alpha")
        os.makedirs(self.wt)
        self.track(self.a, repo=os.path.join(self.dir, "repo"), worktree=self.wt, branch="alpha")

    def test_clean_worktree(self):
        code, out, _ = self.run_cmd("rm", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(out, "killed window 'alpha'\nremoved worktree %s; branch alpha kept\n" % self.wt)
        self.assertNotIn("%1", self.tmux.panes_)
        self.assertEqual(self.ctx.wt.removed, [(os.path.join(self.dir, "repo", "main"), self.wt, False)])
        self.assertFalse(os.path.exists(self.ctx.store.path("%1")))

    def test_dirty_worktree_refused(self):
        self.ctx.git.responses[("status", "--porcelain")] = (0, " M x.py\n")
        self.ctx.git.responses[("status", "--short")] = (0, " M x.py\n")
        code, out, err = self.run_cmd("rm", "alpha")
        self.assertEqual(code, 1)
        self.assertEqual(out, " M x.py\n")
        self.assertIn("uncommitted changes", err)
        self.assertIn("%1", self.tmux.panes_)

    def test_git_status_failure_refuses(self):
        self.ctx.git.responses[("status", "--porcelain")] = (128, "")
        code, _, err = self.run_cmd("rm", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("%1", self.tmux.panes_)

    def test_force(self):
        self.ctx.git.responses[("status", "--porcelain")] = (0, " M x.py\n")
        code, _, _ = self.run_cmd("rm", "--force", "alpha")
        self.assertEqual(code, 0)
        self.assertEqual(self.ctx.wt.removed[0][2], True)

    def test_orchestrator(self):
        self.assertEqual(self.run_cmd("rm", "orch")[2], "fleet: refusing to remove the orchestrator\n")


class New(Fleet):
    def setUp(self):
        super().setUp()
        self.repo = os.path.realpath(os.path.join(self.dir, "repo"))
        os.makedirs(os.path.join(self.repo, "main", ".git"))
        self.new_wt = os.path.join(self.repo, "feat")
        os.makedirs(self.new_wt)

    def test_validation(self):
        for argv, msg in (
            (["new", self.repo, "a/b"], "contains '/'"),
            (["new", self.repo, "orch"], "orchestrator's window"),
            (["new", self.repo, "%3"], "cannot be a worker name"),
            (["new", self.repo, "x", "--prompt-file", "/nope.md"], "no such prompt file"),
            (["new", "/nope", "x"], "no such directory"),
            (["new", self.repo, "alpha"], "already runs in 'fleet'"),
            (["new", self.dir, "x"], "does not follow <repo>/main"),
        ):
            code, _, err = self.run_cmd(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn(msg, err, argv)

    def test_new_branch_from_default(self):
        self.ctx.git.responses[("show-ref", "--verify", "--quiet", "refs/heads/feat")] = (1, "")
        self.ctx.git.responses[("show-ref", "--verify", "--quiet", "refs/remotes/origin/feat")] = (1, "")
        self.ctx.git.responses[("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")] = (0, "origin/main\n")
        self.ctx.wt.switch_result = (0, json.dumps({"path": self.new_wt}))
        pf = os.path.join(self.dir, "task.md")
        open(pf, "w").close()
        code, out, err = self.run_cmd("new", self.repo, "feat", "--prompt-file", pf, "--", "--model", "haiku")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.ctx.wt.calls[0], ("switch", os.path.join(self.repo, "main"), "feat", True, "origin/main"))
        self.assertEqual([c[2] for c in self.ctx.wt.calls[1:]], ["pre-start", "post-start", "post-switch"])
        pane = [p for p in self.tmux.panes_.values() if p.window == "feat"][0]
        rec = st.Store.load(self.ctx.store.path(pane.id))
        self.assertEqual({k: rec[k] for k in ("name", "session", "pane", "dir", "repo", "worktree", "branch", "mode", "state")},
                         {"name": "feat", "session": "fleet", "pane": pane.id, "dir": self.new_wt, "repo": self.repo,
                          "worktree": self.new_wt, "branch": "feat", "mode": "auto", "state": "busy"})
        self.assertEqual(self.tmux.pane_options[(pane.id, "@fleet_name")], "feat")
        launched, submitted = pane.submitted[0][0], pane.submitted[1][0]
        hooks = os.path.join(self.dir, "hooks.json")
        self.assertEqual(launched, "FLEET_STATE_DIR=%s claude /fleet-worker --name feat --permission-mode auto --settings %s --model haiku"
                         % (self.dir, hooks))
        self.assertEqual(submitted, "Read %s and do what it says." % pf)
        self.assertIn("worktree %s on new branch feat from origin/main" % self.new_wt, out)
        settings = json.load(open(hooks))
        self.assertEqual(sorted(settings["hooks"]), sorted(lc.EVENTS))
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"],
                         "FLEET_STATE_DIR=%s /opt/fleet/scripts/fleet-hook Stop" % self.dir)

    def test_prompt_stdin(self):
        self.ctx.wt.list_out = json.dumps({"items": [{"branch": "feat", "worktree": {"path": self.new_wt}}]})
        self.ctx.stdin = io.StringIO("line 1\nline 2\n")
        code, out, err = self.run_cmd("new", self.repo, "feat", "--prompt-stdin")
        self.assertEqual((code, err), (0, ""), out)
        pane = [p for p in self.tmux.panes_.values() if p.window == "feat"][0]
        sent = pane.submitted[1][0]
        path = sent[len("Read "):-len(" and do what it says.")]
        self.assertEqual(sent, "Read %s and do what it says." % path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "line 1\nline 2\n")

    def test_reuses_existing_worktree(self):
        self.ctx.wt.list_out = json.dumps({"items": [{"branch": "feat", "worktree": {"path": self.new_wt}}]})
        code, out, _ = self.run_cmd("new", os.path.join(self.repo, "main"), "feat")
        self.assertEqual(code, 0)
        self.assertIn("reusing worktree %s" % self.new_wt, out)
        self.assertEqual(self.ctx.wt.calls, [])

    def test_existing_branch(self):
        self.ctx.wt.switch_result = (0, json.dumps({"worktree": {"path": self.new_wt}}))
        code, out, _ = self.run_cmd("new", self.repo, "feat")
        self.assertEqual(code, 0)
        self.assertEqual(self.ctx.wt.calls[0][3], False)
        self.assertIn("from existing branch feat", out)

    def test_switch_without_path(self):
        self.ctx.wt.switch_result = (0, "{}")
        code, _, err = self.run_cmd("new", self.repo, "feat")
        self.assertEqual(code, 1)
        self.assertIn("did not report the new worktree's path", err)

    def test_no_default_branch(self):
        for ref in ("refs/heads/feat", "refs/remotes/origin/feat"):
            self.ctx.git.responses[("show-ref", "--verify", "--quiet", ref)] = (1, "")
        self.ctx.git.responses[("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")] = (1, "")
        for b in ("origin/main", "origin/master"):
            self.ctx.git.responses[("rev-parse", "--verify", "--quiet", b)] = (1, "")
        code, _, err = self.run_cmd("new", self.repo, "feat")
        self.assertEqual((code, err), (1, "fleet: cannot find origin's default branch; pass --base REF\n"))

    def test_trust_dialog_exits_3(self):
        def new_window(session, name, cwd):
            p = self.tmux.add(session, name)
            p.screen = fixture("trust_dialog.ansi")
            return p.id
        self.tmux.new_window = new_window
        pf = os.path.join(self.dir, "task.md")
        open(pf, "w").close()
        code, out, err = self.run_cmd("new", "--no-worktree", self.dir, "plain", "--prompt-file", pf)
        self.assertEqual(code, 3)
        self.assertIn("trust-prompt: claude asks whether to trust %s." % os.path.realpath(self.dir), err)
        self.assertIn("fleet keys plain Down Enter", err)
        self.assertIn("prompt not sent", err)

    def test_prompt_box_timeout(self):
        def new_window(session, name, cwd):
            p = self.tmux.add(session, name)
            p.screen = "$ claude\n"
            return p.id
        self.tmux.new_window = new_window
        code, out, err = self.run_cmd("new", "--no-worktree", self.dir, "plain")
        self.assertEqual(code, 1)
        self.assertIn("did not show a prompt box within 60s", err)
        self.assertEqual(out, "$ claude\n")

    def test_prompt_box_timeout_is_wall_clock(self):
        # each screen capture takes a second: the 60 s budget must include that time
        def new_window(session, name, cwd):
            p = self.tmux.add(session, name)
            p.screen = "$ claude\n"
            return p.id
        self.tmux.new_window = new_window
        capture = self.tmux.capture

        def slow_capture(pane, ansi=False, scrollback=0):
            self.clock.t += 1
            return capture(pane, ansi=ansi, scrollback=scrollback)
        self.tmux.capture = slow_capture
        start = self.clock.t
        code, _, _ = self.run_cmd("new", "--no-worktree", self.dir, "plain")
        self.assertEqual(code, 1)
        self.assertLessEqual(self.clock.t - start, 62)


class Up(unittest.TestCase):
    def test_up(self):
        tmux = FakeTmux(own_pane="")
        ctx = make_ctx(tmux, environ={"TMUX": "", "TMUX_PANE": ""})
        code = cli.run(ctx, ["up", "x", "--", "--model", "opus"])
        self.assertEqual(code, 0)
        self.assertEqual(ctx.out.getvalue(), "fleet 'fleet-x' up: orchestrator in window 0 (/work).\n"
                                             "Attach with: tmux attach -t fleet-x\n")
        pane = list(tmux.panes_.values())[0]
        self.assertEqual((pane.session, pane.window), ("fleet-x", "orch"))
        self.assertEqual(tmux.pane_options[(pane.id, "@fleet_role")], "orch")
        self.assertEqual(pane.submitted[0][0],
                         "claude /fleet-orchestrator --name fleet-x --permission-mode auto --allowedTools 'Bash(/opt/fleet/scripts/fleet *)' --model opus")
        self.assertEqual(cli.run(ctx, ["up", "x"]), 1)
        self.assertIn("session 'fleet-x' already exists", ctx.err.getvalue())

    def test_attach_from_a_terminal(self):
        tmux = FakeTmux(own_pane="")
        ctx = make_ctx(tmux, environ={"TMUX": "", "TMUX_PANE": ""})
        ctx.isatty = lambda: True
        cli.run(ctx, ["up"])
        self.assertIn(("attach", "fleet"), tmux.calls)


if __name__ == "__main__":
    unittest.main()
